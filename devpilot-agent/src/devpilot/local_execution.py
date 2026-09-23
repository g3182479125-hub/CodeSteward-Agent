"""Run DevPilot's fixed verification profiles on the local machine."""

from __future__ import annotations

import ctypes
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .execution import (
    MAX_REQUEST_OUTPUT_BYTES,
    ExecutionError,
    ExecutionRequest,
    ExecutionResult,
    execution_bundle_bytes,
    validate_request,
)
from .redaction import redact_execution_output

_WORKSPACE_ROOT = "/workspace/devpilot"
_PYTHON_PROFILES = frozenset(
    {"project-pytest", "python-regression", "python-syntax", "python-run", "project-unittest"}
)
_NODE_PROFILES = frozenset({"project-npm-test", "node-syntax"})
_DEPENDENCY_TIMEOUT_SECONDS = 300
_MAX_CAPTURE_BYTES = MAX_REQUEST_OUTPUT_BYTES * 4


if os.name == "nt":

    class _JobBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _JobBasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class _JobExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JobBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]


class _WindowsProcessJob:
    """Keep one verification process tree together and kill descendants on close."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows process jobs are unavailable on this platform")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_job = self._kernel32.CreateJobObjectW
        create_job.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        create_job.restype = wintypes.HANDLE
        self._handle = create_job(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

        information = _JobExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        set_information = self._kernel32.SetInformationJobObject
        set_information.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        )
        set_information.restype = wintypes.BOOL
        if not set_information(
            self._handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process: subprocess.Popen[bytes]) -> None:
        open_process = self._kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        process_handle = open_process(0x0101, False, process.pid)  # SET_QUOTA | TERMINATE
        if not process_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        assign_process = self._kernel32.AssignProcessToJobObject
        assign_process.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        assign_process.restype = wintypes.BOOL
        try:
            if not assign_process(self._handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            close_handle = self._kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            close_handle(process_handle)

    def terminate_and_wait(self, timeout: float = 5.0) -> bool:
        handle = getattr(self, "_handle", None)
        if not handle:
            return True
        terminate = self._kernel32.TerminateJobObject
        terminate.argtypes = (wintypes.HANDLE, wintypes.UINT)
        terminate.restype = wintypes.BOOL
        if not terminate(handle, 1):
            return False
        query = self._kernel32.QueryInformationJobObject
        query.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        query.restype = wintypes.BOOL
        deadline = time.monotonic() + timeout
        while True:
            accounting = _JobBasicAccountingInformation()
            if not query(
                handle,
                1,  # JobObjectBasicAccountingInformation
                ctypes.byref(accounting),
                ctypes.sizeof(accounting),
                None,
            ):
                return False
            if accounting.ActiveProcesses == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def close(self) -> bool:
        handle = getattr(self, "_handle", None)
        if not handle:
            return True
        close_handle = self._kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        result = bool(close_handle(handle))
        if result:
            self._handle = None
        return result


class _LocalTimeoutExpired(subprocess.TimeoutExpired):
    def __init__(
        self,
        command: list[str],
        timeout: int,
        *,
        output: bytes,
        stderr: bytes,
        safe_to_retry: bool,
    ) -> None:
        super().__init__(command, timeout, output=output, stderr=stderr)
        self.safe_to_retry = safe_to_retry


def _resume_suspended_windows_process(process_id: int) -> None:
    """Resume a process only after its primary thread has joined the job."""

    if os.name != "nt":
        raise OSError("Windows thread control is unavailable on this platform")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())

    entry = _ThreadEntry32()
    entry.dwSize = ctypes.sizeof(entry)
    thread_id: int | None = None
    try:
        next_thread = kernel32.Thread32First
        next_thread.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))
        next_thread.restype = wintypes.BOOL
        next_thread_entry = kernel32.Thread32Next
        next_thread_entry.argtypes = (wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32))
        next_thread_entry.restype = wintypes.BOOL
        has_entry = bool(next_thread(snapshot, ctypes.byref(entry)))
        while has_entry:
            if entry.th32OwnerProcessID == process_id:
                thread_id = entry.th32ThreadID
                break
            has_entry = bool(next_thread_entry(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(snapshot)
    if thread_id is None:
        raise OSError(f"could not find the suspended process thread for PID {process_id}")

    kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenThread.restype = wintypes.HANDLE
    thread_handle = kernel32.OpenThread(0x0002, False, thread_id)  # THREAD_SUSPEND_RESUME
    if not thread_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
        kernel32.ResumeThread.restype = wintypes.DWORD
        previous_suspend_count = kernel32.ResumeThread(thread_handle)
        if previous_suspend_count != 1:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(thread_handle)


def _create_windows_process_job() -> _WindowsProcessJob | None:
    if os.name == "nt":
        return _WindowsProcessJob()
    return None


@dataclass(slots=True)
class _LocalRun:
    temporary_directory: tempfile.TemporaryDirectory[str]
    root: Path
    request: ExecutionRequest
    python_executable: str = sys.executable
    status: str = "ready"


class LocalExecutionBackend:
    """Execute fixed DevPilot profiles against a bounded local snapshot.

    This is not an OS sandbox. Project files are copied to a fresh temporary
    directory and child processes receive a minimal environment, but they run
    with the current user's operating-system permissions.
    """

    name = "local"
    verification_available = True

    def __init__(self, *, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self._runs: dict[str, _LocalRun] = {}
        self._receipts: dict[str, dict[str, Any]] = {}

    def supports_profile(self, profile: str) -> bool:
        return profile in _PYTHON_PROFILES | _NODE_PROFILES | {"react-static"}

    def prepare(
        self, request: ExecutionRequest, *, dependency_fingerprint: str = ""
    ) -> dict[str, str]:
        del dependency_fingerprint  # The caller persists the fingerprint with the Run.
        validate_request(request)
        self._validate_profile(request)
        if request.run_id in self._runs or request.run_id in self._receipts:
            raise ExecutionError("LOCAL_RUN_DUPLICATE", "local run ID has already been used")

        temporary: tempfile.TemporaryDirectory[str] | None = None
        run: _LocalRun | None = None
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            temporary = tempfile.TemporaryDirectory(prefix="devpilot-run-", dir=self.state_dir)
            root = Path(temporary.name).resolve()
            run = _LocalRun(temporary, root, request)
            self._materialize(request, root)
            self._runs[request.run_id] = run
            self._prepare_dependencies(run)
        except ExecutionError as exc:
            cleanup_status = self._record_prepare_failure(request.run_id, run, temporary, exc.code)
            exc.details["cleanup_status"] = cleanup_status
            if cleanup_status != "cleaned":
                exc.details["safe_to_retry"] = False
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            cleanup_status = self._record_prepare_failure(
                request.run_id, run, temporary, "LOCAL_PREPARE_FAILED"
            )
            raise ExecutionError(
                "LOCAL_PREPARE_FAILED",
                "local verification workspace could not be prepared",
                details={
                    "cleanup_status": cleanup_status,
                    "safe_to_retry": False,
                },
            ) from exc

        return {
            "environment_id": request.run_id,
            "status": "ready",
            "execution_location": "local",
        }

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        validate_request(request)
        self._validate_profile(request)
        run = self._runs.get(request.run_id)
        if run is None:
            self.prepare(request)
            run = self._runs[request.run_id]
        if run.request != request and (
            run.request.files != request.files
            or run.request.command != request.command
            or run.request.command_profile != request.command_profile
            or run.request.working_directory != request.working_directory
            or request.prepared_environment_id not in {None, request.run_id}
        ):
            self._cleanup(request.run_id)
            raise ExecutionError(
                "LOCAL_RUN_INPUT_CHANGED", "local Run inputs changed after preparation"
            )

        started = time.monotonic()
        try:
            command, cwd = self._command_and_cwd(run)
            try:
                completed = self._run_process(
                    command,
                    cwd=cwd,
                    env=self._environment(run.root),
                    timeout=request.timeout_seconds,
                    max_capture_bytes=max(_MAX_CAPTURE_BYTES, request.max_output_bytes + 1),
                )
                exit_code = completed.returncode
                stdout = self._decode_output(completed.stdout)
                stderr = self._decode_output(completed.stderr)
            except subprocess.TimeoutExpired as exc:
                stdout = self._decode_output(exc.stdout)
                stderr = self._decode_output(exc.stderr)
                raise ExecutionError(
                    "LOCAL_TIMEOUT",
                    "local verification exceeded its configured timeout",
                    details={
                        "timeout_seconds": request.timeout_seconds,
                        "stdout": redact_execution_output(self._bounded(stdout)),
                        "stderr": redact_execution_output(self._bounded(stderr)),
                        "safe_to_retry": bool(getattr(exc, "safe_to_retry", False)),
                    },
                ) from exc
            except FileNotFoundError as exc:
                raise ExecutionError(
                    "LOCAL_TOOLCHAIN_MISSING",
                    "a required local Python, Node, or npm executable is not installed",
                ) from exc
            truncated = (
                len(stdout.encode("utf-8")) > request.max_output_bytes
                or len(stderr.encode("utf-8")) > request.max_output_bytes
            )
            stdout = redact_execution_output(self._bounded(stdout))
            stderr = redact_execution_output(self._bounded(stderr))
            stdout = self._limit_output(stdout, request.max_output_bytes)
            stderr = self._limit_output(stderr, request.max_output_bytes)
            result = ExecutionResult(
                backend=self.name,
                sandbox_id="",
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                command_profile=request.command_profile,
                bundle_bytes=execution_bundle_bytes(request),
                network_policy="host-default-unrestricted",
                output_truncated=truncated,
                side_effect_count=None,
                permission_violation=None,
                failure_kind="semantic",
                execution_location="local",
            )
            self._receipts[request.run_id] = {
                "run_id": request.run_id,
                "status": "completed",
                "cleanup_status": "pending",
                "result": result,
            }
            return result
        except ExecutionError as exc:
            self._receipts[request.run_id] = {
                "run_id": request.run_id,
                "status": "failed",
                "cleanup_status": exc.details.get("cleanup_status", "pending"),
            }
            raise
        finally:
            cleanup_status = self._cleanup(request.run_id)
            receipt = self._receipts.get(request.run_id)
            if receipt is not None:
                process_cleanup_status = receipt.get("cleanup_status")
                cleanup_failed = (
                    cleanup_status != "cleaned" or process_cleanup_status == "cleanup_failed"
                )
                receipt["cleanup_status"] = "cleanup_failed" if cleanup_failed else "cleaned"
                if cleanup_failed:
                    receipt["status"] = "cleanup_failed"
                    receipt.pop("result", None)
                    if sys.exc_info()[0] is None:
                        raise ExecutionError(
                            "LOCAL_CLEANUP_FAILED",
                            "local verification finished but its temporary workspace "
                            "could not be removed",
                        )

    def get_run_state(self, run_id: str) -> dict[str, Any]:
        return dict(
            self._receipts.get(
                run_id,
                {"run_id": run_id, "status": "unknown", "cleanup_status": "unknown"},
            )
        )

    def _record_prepare_failure(
        self,
        run_id: str,
        run: _LocalRun | None,
        temporary: tempfile.TemporaryDirectory[str] | None,
        error_code: str,
    ) -> str:
        registered = self._runs.pop(run_id, None)
        target = registered or run
        cleanup_status = "cleaned"
        try:
            if target is not None:
                target.temporary_directory.cleanup()
            elif temporary is not None:
                temporary.cleanup()
        except OSError:
            cleanup_status = "cleanup_failed"
        self._receipts[run_id] = {
            "run_id": run_id,
            "status": "failed" if cleanup_status == "cleaned" else "cleanup_failed",
            "cleanup_status": cleanup_status,
            "error_code": error_code,
        }
        return cleanup_status

    @staticmethod
    def _validate_profile(request: ExecutionRequest) -> None:
        if request.command_profile in _PYTHON_PROFILES | _NODE_PROFILES | {"react-static"}:
            tokens = tuple(shlex.split(request.command, posix=True))
            fixed_profiles = {
                "project-pytest": {
                    ("python", "-m", "pytest", "-q"),
                    ("python3", "-m", "pytest", "-q"),
                },
                "python-regression": {
                    ("python", "-m", "pytest", "-q"),
                    ("python3", "-m", "pytest", "-q"),
                },
                "project-unittest": {
                    ("python", "-m", "unittest", "discover", "-v"),
                    ("python3", "-m", "unittest", "discover", "-v"),
                },
                "project-npm-test": {("npm", "test")},
            }
            allowed = fixed_profiles.get(request.command_profile)
            if allowed is not None and tokens not in allowed:
                raise ExecutionError(
                    "LOCAL_COMMAND_PROFILE_MISMATCH",
                    "command does not match its server-selected local profile",
                )
            return
        raise ExecutionError(
            "LOCAL_PROFILE_UNSUPPORTED", "local execution profile is not supported"
        )

    @staticmethod
    def _materialize(request: ExecutionRequest, root: Path) -> None:
        for item in request.files:
            relative = Path(item.relative_path.replace("\\", "/"))
            target = (root / relative).resolve()
            if root not in target.parents:
                raise ExecutionError(
                    "LOCAL_INPUT_PATH_INVALID", "snapshot path escaped the local workspace"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.content)

    def _prepare_dependencies(self, run: _LocalRun) -> None:
        request = run.request
        python_needs_environment = request.command_profile in _PYTHON_PROFILES and (
            bool(request.dependency_profile)
            or request.command_profile in {"project-pytest", "python-regression"}
        )
        if python_needs_environment:
            venv_path = run.root / ".devpilot-venv"
            self._run_setup(
                [sys.executable, "-m", "venv", str(venv_path)],
                run,
                "LOCAL_ENVIRONMENT_CREATE_FAILED",
                "local Python environment could not be created",
            )
            run.python_executable = str(
                venv_path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            )
            working_directory = self._working_directory(run)
            if request.dependency_profile == "python-requirements":
                manifest = working_directory / "requirements.txt"
                if not manifest.is_file():
                    raise ExecutionError(
                        "LOCAL_DEPENDENCY_MANIFEST_MISSING",
                        "requirements.txt is missing from the project snapshot",
                    )
                self._run_setup(
                    [run.python_executable, "-m", "pip", "install", "-r", str(manifest)],
                    run,
                    "LOCAL_DEPENDENCY_INSTALL_FAILED",
                    "local Python dependencies could not be installed",
                )
            elif request.dependency_profile == "python-project":
                self._run_setup(
                    [run.python_executable, "-m", "pip", "install", "-e", str(working_directory)],
                    run,
                    "LOCAL_DEPENDENCY_INSTALL_FAILED",
                    "local Python project dependencies could not be installed",
                )
            if request.command_profile in {"project-pytest", "python-regression"}:
                self._run_setup(
                    [run.python_executable, "-m", "pip", "install", "pytest"],
                    run,
                    "LOCAL_DEPENDENCY_INSTALL_FAILED",
                    "pytest could not be installed in the local project environment",
                )

        if request.dependency_profile == "node-npm-ci":
            self._run_setup(
                self._npm_command("ci", "--ignore-scripts"),
                run,
                "LOCAL_DEPENDENCY_INSTALL_FAILED",
                "local Node dependencies could not be installed",
            )

    def _run_setup(
        self,
        command: list[str],
        run: _LocalRun,
        code: str,
        message: str,
    ) -> None:
        try:
            completed = self._run_process(
                command,
                cwd=self._working_directory(run),
                env=self._environment(run.root),
                timeout=_DEPENDENCY_TIMEOUT_SECONDS,
                max_capture_bytes=4_000,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExecutionError(code, message) from exc
        if completed.returncode != 0:
            raise ExecutionError(
                code,
                message,
                details={
                    "exit_code": completed.returncode,
                    "stdout": self._limit_output(
                        redact_execution_output(self._decode_output(completed.stdout)), 2_000
                    ),
                    "stderr": self._limit_output(
                        redact_execution_output(self._decode_output(completed.stderr)), 2_000
                    ),
                },
            )

    def _command_and_cwd(self, run: _LocalRun) -> tuple[list[str], Path]:
        request = run.request
        cwd = self._working_directory(run)
        tokens = shlex.split(request.command, posix=True)
        profile = request.command_profile
        if profile in {"project-pytest", "python-regression"}:
            command = [run.python_executable, "-m", "pytest", "-q"]
        elif profile == "project-unittest":
            command = [
                run.python_executable,
                "-m",
                "unittest",
                "discover",
                "-v",
            ]
        elif profile == "project-npm-test":
            command = self._npm_command("test")
        elif profile == "python-syntax":
            paths = self._snapshot_paths(tokens[3:], run)
            command = [run.python_executable, "-m", "py_compile", *paths]
        elif profile == "python-run":
            command = [run.python_executable, self._snapshot_paths(tokens[1:], run)[0]]
        elif profile == "node-syntax":
            command = ["node", "--check", *self._snapshot_paths(tokens[2:], run)]
        elif profile == "react-static":
            paths = self._snapshot_paths(tokens[1:], run)
            failed = [
                path
                for path in paths
                if "event.preventDefault()" not in Path(path).read_text(encoding="utf-8")
            ]
            return [
                sys.executable,
                "-c",
                "import sys; raise SystemExit(bool(sys.argv[1:]))",
                *failed,
            ], cwd
        else:
            raise ExecutionError(
                "LOCAL_PROFILE_UNSUPPORTED", "local execution profile is not supported"
            )
        return command, cwd

    @staticmethod
    def _snapshot_paths(tokens: list[str], run: _LocalRun) -> list[str]:
        paths: list[str] = []
        for token in tokens:
            prefix = _WORKSPACE_ROOT + "/"
            if not token.startswith(prefix):
                raise ExecutionError("LOCAL_COMMAND_FORBIDDEN", "command path is outside snapshot")
            path = (run.root / token[len(prefix) :]).resolve()
            if run.root not in path.parents or not path.is_file():
                raise ExecutionError("LOCAL_COMMAND_FORBIDDEN", "command path is outside snapshot")
            paths.append(str(path))
        if not paths:
            raise ExecutionError("LOCAL_COMMAND_FORBIDDEN", "command has no snapshot files")
        return paths

    @staticmethod
    def _working_directory(run: _LocalRun) -> Path:
        path = (run.root / run.request.working_directory).resolve()
        if run.root not in path.parents and path != run.root:
            raise ExecutionError(
                "LOCAL_WORKING_DIRECTORY_INVALID", "working directory escaped local snapshot"
            )
        if not path.is_dir():
            raise ExecutionError(
                "LOCAL_WORKING_DIRECTORY_INVALID", "working directory is missing from snapshot"
            )
        return path

    @staticmethod
    def _npm_command(*arguments: str) -> list[str]:
        if os.name == "nt":
            # npm is normally exposed as npm.cmd on Windows. Invoke that fixed
            # command through cmd.exe; caller-provided shell text is never used.
            command_processor = os.environ.get("COMSPEC", r"C:\\Windows\\System32\\cmd.exe")
            return [command_processor, "/d", "/s", "/c", "npm " + " ".join(arguments)]
        executable = shutil.which("npm")
        if executable is None:
            raise FileNotFoundError("npm")
        return [executable, *arguments]

    @staticmethod
    def _environment(root: Path) -> dict[str, str]:
        npm_config = root / "empty.npmrc"
        npm_config.touch(exist_ok=True)
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CONFIG_FILE": "NUL" if os.name == "nt" else os.devnull,
            "PIP_CACHE_DIR": str(root / ".pip-cache"),
            "NPM_CONFIG_USERCONFIG": str(npm_config),
            "NPM_CONFIG_CACHE": str(root / ".npm-cache"),
            "TMP": str(root),
            "TEMP": str(root),
        }
        if os.name == "nt":
            env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
            env["COMSPEC"] = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
            env["PATHEXT"] = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
            env["USERPROFILE"] = str(root)
        else:
            env["HOME"] = str(root)
        return env

    @staticmethod
    def _run_process(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: int,
        max_capture_bytes: int,
    ) -> subprocess.CompletedProcess[bytes]:
        job = _create_windows_process_job()
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | 0x00000004  # CREATE_SUSPENDED is not exposed by subprocess on all Pythons.
            if os.name == "nt"
            else 0
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=os.name != "nt",
                creationflags=creationflags,
            )
        except OSError as exc:
            if job is not None and not job.close():
                raise ExecutionError(
                    "LOCAL_PROCESS_CLEANUP_FAILED",
                    "Windows could not close the unused verification process job",
                    details={"cleanup_status": "cleanup_failed", "safe_to_retry": False},
                ) from exc
            raise
        if job is not None:
            try:
                job.assign(process)
                _resume_suspended_windows_process(process.pid)
            except OSError as exc:
                job_stopped = job.terminate_and_wait()
                job_closed = job.close()
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=5)
                    process_stopped = True
                except subprocess.TimeoutExpired:
                    try:
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            capture_output=True,
                            timeout=5,
                            check=False,
                            shell=False,
                        )
                    except (OSError, subprocess.SubprocessError):
                        pass
                    try:
                        process.wait(timeout=5)
                        process_stopped = True
                    except subprocess.TimeoutExpired:
                        process_stopped = False
                cleanup_confirmed = job_stopped and job_closed and process_stopped
                raise ExecutionError(
                    "LOCAL_PROCESS_CONTAINMENT_FAILED"
                    if cleanup_confirmed
                    else "LOCAL_PROCESS_CLEANUP_FAILED",
                    "Windows could not track the local verification process tree"
                    if cleanup_confirmed
                    else "Windows could not confirm the local process tree was stopped",
                    details={
                        "cleanup_status": "cleaned" if cleanup_confirmed else "cleanup_failed",
                        "safe_to_retry": False,
                    },
                ) from exc
        captured = {"stdout": bytearray(), "stderr": bytearray()}

        def drain(name: str, stream: Any) -> None:
            try:
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        return
                    buffer = captured[name]
                    buffer.extend(chunk)
                    if len(buffer) > max_capture_bytes:
                        del buffer[: len(buffer) - max_capture_bytes]
            except (OSError, ValueError):
                # Closing a pipe after a descendant inherited it can unblock
                # this reader once the owning process has exited.
                return

        readers = [
            threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        safe_to_retry = False
        process_cleanup_failed = False
        job_processes_stopped = False

        def wait_for_direct_process() -> bool:
            try:
                process.wait(timeout=5)
                return True
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=5)
                    return True
                except subprocess.TimeoutExpired:
                    return False

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if job is not None:
                job_processes_stopped = job.terminate_and_wait()
                safe_to_retry = job_processes_stopped
                if not wait_for_direct_process():
                    if not safe_to_retry:
                        try:
                            taskkill = subprocess.run(
                                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                capture_output=True,
                                timeout=5,
                                check=False,
                                shell=False,
                            )
                            process_cleanup_failed = taskkill.returncode != 0
                        except (OSError, subprocess.SubprocessError):
                            process_cleanup_failed = True
                        process_cleanup_failed = not wait_for_direct_process()
                        job_processes_stopped = job.terminate_and_wait()
                        safe_to_retry = job_processes_stopped
                        process_cleanup_failed = process_cleanup_failed or not job_processes_stopped
                    else:
                        process_cleanup_failed = True
            else:
                safe_to_retry = LocalExecutionBackend._terminate_posix_process_group(process.pid)
                process_cleanup_failed = not wait_for_direct_process()
        finally:
            if job is not None:
                if not job_processes_stopped:
                    job_processes_stopped = job.terminate_and_wait()
                handle_closed = job.close()
                process_cleanup_failed = process_cleanup_failed or not (
                    job_processes_stopped and handle_closed
                )
            if os.name != "nt" and not LocalExecutionBackend._terminate_posix_process_group(
                process.pid
            ):
                process_cleanup_failed = True
            if os.name != "nt" and not wait_for_direct_process():
                process_cleanup_failed = True
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            for reader in readers:
                reader.join(timeout=5)
        stdout = bytes(captured["stdout"])
        stderr = bytes(captured["stderr"])
        if process_cleanup_failed:
            raise ExecutionError(
                "LOCAL_PROCESS_CLEANUP_FAILED",
                "the local verification process tree could not be confirmed stopped",
            )
        if timed_out:
            raise _LocalTimeoutExpired(
                command,
                timeout,
                output=stdout,
                stderr=stderr,
                safe_to_retry=safe_to_retry,
            )
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    @staticmethod
    def _terminate_posix_process_group(process_id: int) -> bool:
        killpg = cast(Any, os).killpg
        sigkill = cast(Any, signal).SIGKILL
        try:
            killpg(process_id, sigkill)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                killpg(process_id, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            time.sleep(0.05)
        return False

    @staticmethod
    def _decode_output(value: bytes | str | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value or ""

    @staticmethod
    def _bounded(value: str) -> str:
        raw = value.encode("utf-8")
        if len(raw) <= _MAX_CAPTURE_BYTES:
            return value
        return raw[-_MAX_CAPTURE_BYTES:].decode("utf-8", errors="replace")

    @staticmethod
    def _limit_output(value: str, maximum: int) -> str:
        raw = value.encode("utf-8")
        if len(raw) <= maximum:
            return value
        return raw[-maximum:].decode("utf-8", errors="replace")

    def _cleanup(self, run_id: str) -> str:
        run = self._runs.pop(run_id, None)
        if run is None:
            return "cleaned"
        try:
            run.temporary_directory.cleanup()
        except OSError:
            run.status = "cleanup_failed"
            return "cleanup_failed"
        run.status = "cleaned"
        return "cleaned"
