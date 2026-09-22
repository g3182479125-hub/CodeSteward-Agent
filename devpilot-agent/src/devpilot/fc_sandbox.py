"""Alibaba FC Agent Sandbox adapter through the E2B-compatible Python SDK.

The dependency is optional and imported lazily.  This keeps the local P2B
approval service usable without cloud credentials or network access.  The
adapter is an execution port only; it is not registered as a nanobot Tool and
does not mutate the local project directory.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shlex
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from .execution import (
    MAX_REQUEST_OUTPUT_BYTES,
    ExecutionBackend,
    ExecutionError,
    ExecutionRequest,
    ExecutionResult,
    execution_bundle_bytes,
    validate_request,
)
from .redaction import redact_execution_output

SANDBOX_ROOT = "/workspace/devpilot"
MAX_CLEANUP_ATTEMPTS = 3
PYTEST_INSTALL_COMMAND = "python -m pip install --disable-pip-version-check pytest"
PYTEST_INSTALL_TIMEOUT_SECONDS = 120
DEPENDENCY_INSTALL_TIMEOUT_SECONDS = 300
_PERSIST_LOCK = RLock()
FC_VERIFICATION_PROFILES = frozenset(
    {
        "python-syntax",
        "python-run",
        "python-regression",
        "node-syntax",
        "project-pytest",
        "project-npm-test",
    }
)


def _provider_error(stage: str, exc: Exception) -> ExecutionError:
    """Map provider failures to stable, secret-free adapter error codes."""

    detail = str(exc)
    normalized = detail.casefold()
    if "sessionnetworkpolicy" in normalized:
        return ExecutionError(
            "FC_CAPABILITY_UNSUPPORTED",
            "FC endpoint does not support the required Sandbox network policy",
        )
    if any(marker in normalized for marker in ("401", "403", "unauthorized", "forbidden")):
        return ExecutionError("FC_AUTH_FAILED", "FC Sandbox authentication was rejected")
    if any(marker in normalized for marker in ("timeout", "timed out", "deadline exceeded")):
        return ExecutionError("FC_TIMEOUT", f"FC Sandbox {stage} timed out")
    codes = {
        "create": "FC_SANDBOX_CREATE_FAILED",
        "upload": "FC_UPLOAD_FAILED",
        "execute": "FC_EXECUTION_FAILED",
        "cleanup": "FC_CLEANUP_FAILED",
    }
    return ExecutionError(codes[stage], f"FC Sandbox {stage} failed")


@dataclass(frozen=True, slots=True)
class FCSandboxConfig:
    """Connection settings loaded from environment, never source-controlled."""

    api_key: str
    api_url: str
    domain: str
    template: str = "code-interpreter-v1"
    install_pytest: bool = True
    allow_network: bool = False
    max_timeout_seconds: int = 900
    region: str = ""
    network_policy_mode: str = "required"
    allowed_project_paths: tuple[str, ...] = ()
    state_dir: str | None = None

    @classmethod
    def from_environment(cls) -> FCSandboxConfig:
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ExecutionError("FC_CONFIG_MISSING", f"missing {name}")
            return value

        try:
            max_timeout_seconds = int(os.environ.get("DEVPILOT_FC_MAX_TIMEOUT_SECONDS", "900"))
        except ValueError as exc:
            raise ExecutionError(
                "FC_CONFIG_INVALID", "DEVPILOT_FC_MAX_TIMEOUT_SECONDS must be an integer"
            ) from exc
        if not 1 <= max_timeout_seconds <= 900:
            raise ExecutionError(
                "FC_CONFIG_INVALID", "DEVPILOT_FC_MAX_TIMEOUT_SECONDS must be between 1 and 900"
            )
        network_policy_mode = os.environ.get("DEVPILOT_FC_NETWORK_POLICY_MODE", "required").strip()
        if network_policy_mode not in {"required", "provider-default"}:
            raise ExecutionError(
                "FC_CONFIG_INVALID",
                "DEVPILOT_FC_NETWORK_POLICY_MODE must be required or provider-default",
            )
        install_pytest_value = os.environ.get("DEVPILOT_FC_INSTALL_PYTEST", "1").strip()
        if install_pytest_value not in {"0", "1"}:
            raise ExecutionError(
                "FC_CONFIG_INVALID",
                "DEVPILOT_FC_INSTALL_PYTEST must be 0 or 1",
            )
        domain = required("E2B_DOMAIN")
        allowed_project_paths = tuple(
            str(os.path.abspath(item.strip()))
            for item in os.environ.get("DEVPILOT_FC_ALLOWED_PROJECTS", "").split(os.pathsep)
            if item.strip()
        )
        return cls(
            api_key=required("E2B_API_KEY"),
            api_url=required("E2B_API_URL"),
            domain=domain,
            template=os.environ.get("DEVPILOT_FC_TEMPLATE", "code-interpreter-v1").strip()
            or "code-interpreter-v1",
            install_pytest=install_pytest_value == "1",
            allow_network=os.environ.get("DEVPILOT_FC_ALLOW_NETWORK", "0") == "1",
            max_timeout_seconds=max_timeout_seconds,
            region=os.environ.get("DEVPILOT_FC_REGION", domain.split(".", 1)[0]).strip(),
            network_policy_mode=network_policy_mode,
            allowed_project_paths=allowed_project_paths,
            state_dir=os.environ.get("DEVPILOT_FC_STATE_DIR")
            or str(
                Path(
                    os.environ.get(
                        "DEVPILOT_DATA_DIR",
                        str(Path.home() / ".nanobot-skill-improvement"),
                    )
                )
                / "fc-runs"
            ),
        )


def _sandbox_class() -> Any:
    try:
        module = importlib.import_module("e2b_code_interpreter")
        return module.Sandbox
    except (ImportError, AttributeError) as exc:
        raise ExecutionError(
            "FC_SDK_MISSING", "install the optional fc-sandbox dependencies before using FC backend"
        ) from exc


def _sandbox_id(sandbox: Any) -> str:
    value = getattr(sandbox, "sandbox_id", None) or getattr(sandbox, "id", None)
    return str(value or "unknown-sandbox")


@contextmanager
def _state_file_lock(path: Path) -> Iterator[None]:
    """Serialize snapshot reads and replacements across backend processes."""

    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@dataclass(slots=True)
class _FCSandboxRun:
    run_id: str
    sandbox_id: str | None = None
    status: str = "starting"
    handle: Any | None = None
    result: ExecutionResult | None = None
    error_code: str | None = None
    cleanup_error: str | None = None
    cancel_requested: bool = False
    cleanup_done: bool = False
    cleanup_attempts: int = 0
    revision: int = 0


class FCSandboxBackend:
    """Run an explicit verification bundle in a short-lived FC Sandbox.

    ``run`` remains the execution-port entry point used by the control plane.
    Lifecycle methods provide bounded cancellation, collection, and recovery
    receipts without creating a second execution framework.
    """

    name = "fc-agent-sandbox"

    def __init__(self, config: FCSandboxConfig) -> None:
        self.config = config
        self._lock = RLock()
        self._runs: dict[str, _FCSandboxRun] = {}

    @property
    def verification_available(self) -> bool:
        """Expose the explicitly opted-in degraded FC mode to the control plane."""

        return self.config.network_policy_mode == "provider-default"

    @property
    def supported_profiles(self) -> frozenset[str]:
        """Profiles with real FC evidence; React static remains Fake-only."""

        return FC_VERIFICATION_PROFILES

    @property
    def candidate_execution_available(self) -> bool:
        """Expose the dedicated model-candidate FC execution contract."""

        return self.config.network_policy_mode in {"required", "provider-default"}

    def candidate_execution_contract(self) -> tuple[str, str, str, str]:
        """Return the immutable contract required by model-candidate trials."""

        return (
            self.name,
            "model-candidate",
            self.config.template,
            self.config.network_policy_mode,
        )

    def supports_profile(self, profile: str) -> bool:
        return profile in self.supported_profiles

    def project_allowed(self, project_path: str) -> bool:
        """Require the project to sit inside an explicit operator allowlist root."""

        try:
            resolved = os.path.normcase(os.path.abspath(project_path))
        except (TypeError, ValueError):
            return False
        for allowed_root in self.config.allowed_project_paths:
            try:
                normalized_root = os.path.normcase(os.path.abspath(allowed_root))
                if os.path.commonpath((resolved, normalized_root)) == normalized_root:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _should_install_pytest(self, request: ExecutionRequest) -> bool:
        return self.config.install_pytest and request.command_profile in {
            "project-pytest",
            "python-regression",
        }

    @staticmethod
    def _sandbox_working_directory(request: ExecutionRequest) -> str:
        if not request.working_directory:
            return SANDBOX_ROOT
        normalized = request.working_directory.replace("\\", "/")
        return f"{SANDBOX_ROOT}/{normalized}"

    @staticmethod
    def _dependency_command(request: ExecutionRequest) -> str | None:
        working_directory = FCSandboxBackend._sandbox_working_directory(request)
        if request.dependency_profile == "python-requirements":
            return (
                "python -m pip install --disable-pip-version-check -r "
                f"{shlex.quote(working_directory + '/requirements.txt')}"
            )
        if request.dependency_profile == "python-project":
            return (
                "python -m pip install --disable-pip-version-check -e "
                f"{shlex.quote(working_directory)}"
            )
        if request.dependency_profile == "node-npm-ci":
            return "npm ci --ignore-scripts"
        return None

    @staticmethod
    def _should_install_dependencies(request: ExecutionRequest) -> bool:
        return bool(request.dependency_profile)

    def _install_dependencies(self, sandbox_handle: Any, request: ExecutionRequest) -> None:
        command = self._dependency_command(request)
        if command is None:
            return
        try:
            result = sandbox_handle.commands.run(
                command,
                timeout=DEPENDENCY_INSTALL_TIMEOUT_SECONDS,
                cwd=self._sandbox_working_directory(request),
            )
        except Exception as exc:
            if not isinstance(getattr(exc, "exit_code", None), int):
                raise ExecutionError(
                    "FC_DEPENDENCY_INSTALL_FAILED",
                    "FC Sandbox could not prepare project dependencies",
                ) from exc
            result = exc
        exit_code = getattr(result, "exit_code", None)
        if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code != 0:
            raise ExecutionError(
                "FC_DEPENDENCY_INSTALL_FAILED",
                "FC Sandbox could not prepare project dependencies",
                details={"exit_code": exit_code},
            )

    def _install_pytest(self, sandbox_handle: Any) -> None:
        """Install the fixed test runner dependency inside this short-lived sandbox."""

        try:
            try:
                result = sandbox_handle.commands.run(
                    PYTEST_INSTALL_COMMAND,
                    timeout=PYTEST_INSTALL_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                if not isinstance(getattr(exc, "exit_code", None), int):
                    raise
                result = exc
        except Exception as exc:
            raise ExecutionError(
                "FC_PYTEST_INSTALL_FAILED",
                "FC Sandbox could not install pytest",
            ) from exc
        exit_code = getattr(result, "exit_code", None)
        if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code != 0:
            raise ExecutionError(
                "FC_PYTEST_INSTALL_FAILED",
                "FC Sandbox could not install pytest",
                details={"exit_code": exit_code},
            )

    def _reserve_run(self, run_id: str) -> _FCSandboxRun:
        with self._lock:
            if run_id in self._runs:
                raise ExecutionError("FC_RUN_DUPLICATE", "FC run ID has already been used")
            path = self._state_path(run_id)
            if path is None:
                state = _FCSandboxRun(run_id=run_id)
                self._runs[run_id] = state
                return state
            with _PERSIST_LOCK, _state_file_lock(path):
                if path.is_file():
                    persisted = self._load_persisted_state_unlocked(run_id)
                    if persisted is not None:
                        raise ExecutionError(
                            "FC_RUN_DUPLICATE", "FC run ID has already been used"
                        )
                    raise ExecutionError(
                        "FC_RUN_STATE_UNAVAILABLE",
                        "FC run state exists but cannot be read safely",
                    )
                state = _FCSandboxRun(run_id=run_id)
                self._runs[run_id] = state
                self._persist_state_unlocked(state, path)
                return state

    def _state_payload(self, state: _FCSandboxRun) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": state.run_id,
            "sandbox_id": state.sandbox_id,
            "status": state.status,
            "error_code": state.error_code,
            "cleanup_error": state.cleanup_error,
            "cleanup_attempts": state.cleanup_attempts,
            "cleanup_status": (
                "cleaned"
                if state.cleanup_done
                else "manual_pending"
                if state.cleanup_attempts >= MAX_CLEANUP_ATTEMPTS
                else "pending"
            ),
            "network_policy": self.config.network_policy_mode,
        }
        if state.result is not None:
            payload["result"] = {
                "backend": state.result.backend,
                "sandbox_id": state.result.sandbox_id,
                "exit_code": state.result.exit_code,
                "stdout": redact_execution_output(
                    state.result.stdout[-MAX_REQUEST_OUTPUT_BYTES:]
                ),
                "stderr": redact_execution_output(
                    state.result.stderr[-MAX_REQUEST_OUTPUT_BYTES:]
                ),
                "duration_ms": state.result.duration_ms,
                "command_profile": state.result.command_profile,
                "bundle_bytes": state.result.bundle_bytes,
                "region": state.result.region,
                "template": state.result.template,
                "network_policy": state.result.network_policy,
                "output_truncated": state.result.output_truncated,
                "side_effect_count": state.result.side_effect_count,
                "permission_violation": state.result.permission_violation,
                "failure_kind": state.result.failure_kind,
            }
        return payload

    def _state_path(self, run_id: str) -> Path | None:
        if not self.config.state_dir:
            return None
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        return Path(self.config.state_dir).expanduser().resolve() / f"{digest}.json"

    def _persist_state_unlocked(self, state: _FCSandboxRun, path: Path) -> None:
        """Write one restart-safe provider snapshot while its file lock is held."""

        # Backend instances can overlap briefly during restart recovery.  Keep the
        # temporary-file/replace pair atomic within the process so Windows does not
        # reject two concurrent replacements of the same destination.
        temporary_path: Path | None = None
        current_revision = self._snapshot_revision(path)
        if current_revision is not None and current_revision > state.revision:
            return
        state.revision = max(state.revision, current_revision or 0) + 1
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix=".fc-run-", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                payload = self._state_payload(state)
                payload["revision"] = state.revision
                json.dump(
                    payload, temporary, ensure_ascii=False, sort_keys=True
                )
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _snapshot_revision(path: Path) -> int | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise OSError("FC run state cannot be read safely") from exc
        if not isinstance(payload, dict):
            raise OSError("FC run state cannot be read safely")
        try:
            revision = int(payload.get("revision", 0))
        except (TypeError, ValueError) as exc:
            raise OSError("FC run state revision is invalid") from exc
        if revision < 0:
            raise OSError("FC run state revision is invalid")
        return revision

    def _persist_state(self, state: _FCSandboxRun) -> None:
        """Persist restart-safe provider metadata without credentials or handles."""

        path = self._state_path(state.run_id)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with _PERSIST_LOCK, _state_file_lock(path):
            self._persist_state_unlocked(state, path)

    def _load_persisted_state_unlocked(self, run_id: str) -> _FCSandboxRun | None:
        """Load a snapshot while its file lock is held."""

        path = self._state_path(run_id)
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict) or payload.get("run_id") != run_id:
            return None
        result_payload = payload.get("result")
        result = None
        if isinstance(result_payload, dict):
            try:
                result = ExecutionResult(
                    backend=str(result_payload["backend"]),
                    sandbox_id=str(result_payload["sandbox_id"]),
                    exit_code=int(result_payload["exit_code"]),
                    stdout=str(result_payload.get("stdout", "")),
                    stderr=str(result_payload.get("stderr", "")),
                    duration_ms=int(result_payload.get("duration_ms", 0)),
                    command_profile=str(result_payload.get("command_profile", "")),
                    bundle_bytes=int(result_payload.get("bundle_bytes", 0)),
                    region=str(result_payload.get("region", "")),
                    template=str(result_payload.get("template", "")),
                    network_policy=str(result_payload.get("network_policy", "")),
                    output_truncated=bool(result_payload.get("output_truncated", False)),
                    side_effect_count=(
                        int(result_payload["side_effect_count"])
                        if isinstance(result_payload.get("side_effect_count"), int)
                        and not isinstance(result_payload.get("side_effect_count"), bool)
                        else None
                    ),
                    permission_violation=(
                        result_payload["permission_violation"]
                        if isinstance(result_payload.get("permission_violation"), bool)
                        else None
                    ),
                    failure_kind=str(result_payload.get("failure_kind", "unknown")),
                )
            except (KeyError, TypeError, ValueError):
                result = None
        try:
            cleanup_attempts = int(payload.get("cleanup_attempts", 0))
        except (TypeError, ValueError):
            cleanup_attempts = 0
        try:
            revision = int(payload.get("revision", 0))
        except (TypeError, ValueError):
            revision = 0
        return _FCSandboxRun(
            run_id=run_id,
            sandbox_id=str(payload["sandbox_id"]) if payload.get("sandbox_id") else None,
            status=str(payload.get("status", "unknown")),
            result=result,
            error_code=str(payload["error_code"]) if payload.get("error_code") else None,
            cleanup_error=(
                str(payload["cleanup_error"]) if payload.get("cleanup_error") else None
            ),
            cleanup_attempts=cleanup_attempts,
            cleanup_done=payload.get("cleanup_status") == "cleaned",
            revision=max(revision, 0),
        )

    def _load_persisted_state(self, run_id: str) -> _FCSandboxRun | None:
        path = self._state_path(run_id)
        if path is None or not path.is_file():
            return None
        with _PERSIST_LOCK, _state_file_lock(path):
            return self._load_persisted_state_unlocked(run_id)

    def _get_or_load_state(self, run_id: str) -> _FCSandboxRun | None:
        state = self._runs.get(run_id)
        if state is not None:
            return state
        state = self._load_persisted_state(run_id)
        if state is not None:
            self._runs[run_id] = state
        return state

    def get_run_state(self, run_id: str) -> dict[str, Any]:
        """Return a bounded lifecycle receipt, including a durable restart snapshot."""

        with self._lock:
            state = self._get_or_load_state(run_id)
            if state is None:
                return {
                    "run_id": run_id,
                    "sandbox_id": None,
                    "status": "unknown",
                    "error_code": "FC_RUN_NOT_TRACKED",
                    "cleanup_error": None,
                    "cleanup_attempts": 0,
                    "cleanup_status": "unknown",
                    "network_policy": self.config.network_policy_mode,
                }
            return self._state_payload(state)

    def collect(self, run_id: str) -> dict[str, Any]:
        """Collect the persisted in-process result without starting a task."""

        return self.get_run_state(run_id)

    def cancel(self, run_id: str) -> dict[str, Any]:
        """Cancel one active run; repeated cancellation is idempotent."""

        with self._lock:
            state = self._get_or_load_state(run_id)
            if state is None:
                return self.get_run_state(run_id)
            if state.status not in {"starting", "running"} and not state.cleanup_error:
                return self._state_payload(state)
            state.cancel_requested = True
            handle = state.handle
        if handle is None:
            recovered = self.recover(
                run_id,
                state.sandbox_id,
                _for_cleanup=True,
            )
            with self._lock:
                refreshed = self._get_or_load_state(run_id)
                handle = refreshed.handle if refreshed is not None else None
            if handle is None:
                return recovered
        with self._lock:
            if state.cleanup_attempts >= MAX_CLEANUP_ATTEMPTS:
                return self._state_payload(state)
            state.cleanup_attempts += 1
        try:
            handle.kill()
        except Exception as exc:
            with self._lock:
                if state.status in {"starting", "running"}:
                    state.status = "cancelled"
                state.cleanup_error = _provider_error("cleanup", exc).code
                self._persist_state(state)
            return self.get_run_state(run_id)
        with self._lock:
            state.cleanup_done = True
            state.cleanup_error = None
            if state.status in {"starting", "running"}:
                state.status = "cancelled"
            self._persist_state(state)
        return self.get_run_state(run_id)

    def cleanup(self, run_id: str) -> dict[str, Any]:
        """Clean up an active sandbox without creating a replacement task."""

        with self._lock:
            state = self._get_or_load_state(run_id)
            needs_recovery = bool(
                state is not None
                and state.handle is None
                and state.cleanup_error
                and state.sandbox_id
            )
        if needs_recovery:
            self.recover(run_id, _for_cleanup=True)
        return self.cancel(run_id)

    def recover(
        self,
        run_id: str,
        sandbox_id: str | None = None,
        *,
        _for_cleanup: bool = False,
    ) -> dict[str, Any]:
        """Reconnect to a provider sandbox after a process restart.

        Recovery only uses the persisted sandbox identifier or an explicitly
        supplied one. It never creates a sandbox or replays the command.
        """

        if not run_id:
            raise ExecutionError("FC_RECOVERY_INVALID", "run_id is required")
        path = self._state_path(run_id)
        persistence_lock = _PERSIST_LOCK if path is not None else nullcontext()
        state_lock = _state_file_lock(path) if path is not None else nullcontext()
        with self._lock, persistence_lock, state_lock:
            existing = self._runs.get(run_id)
            if existing is None and path is not None:
                existing = self._load_persisted_state_unlocked(run_id)
                if existing is not None:
                    self._runs[run_id] = existing
            if existing is not None and existing.handle is not None:
                return self._state_payload(existing)
            if (
                existing is not None
                and existing.status in {"completed", "failed", "cancelled"}
                and (
                    existing.result is not None
                    or (existing.status == "cancelled" and not existing.cleanup_error)
                )
            ):
                return self._state_payload(existing)
            if sandbox_id is None and existing is not None:
                sandbox_id = existing.sandbox_id
            if not sandbox_id:
                raise ExecutionError("FC_RECOVERY_INVALID", "sandbox_id is required")
            try:
                sandbox_cls = _sandbox_class()
                connector = getattr(sandbox_cls, "connect", None)
                if not callable(connector):
                    raise RuntimeError("FC SDK does not expose Sandbox.connect")
                handle: Any = connector(
                    sandbox_id,
                    api_key=self.config.api_key,
                    api_url=self.config.api_url,
                    domain=self.config.domain,
                )
                running = bool(handle.is_running())
            except Exception:
                state = existing or _FCSandboxRun(run_id=run_id)
                self._runs[run_id] = state
                state.sandbox_id = sandbox_id
                state.status = "unknown"
                state.error_code = "FC_RECOVERY_UNKNOWN"
                if path is not None:
                    self._persist_state_unlocked(state, path)
                return self._state_payload(state)
            state = existing or _FCSandboxRun(run_id=run_id)
            self._runs[run_id] = state
            state.sandbox_id = sandbox_id
            if running or (_for_cleanup and existing is not None and existing.cleanup_error):
                state.status = "running"
                state.handle = handle
                if running:
                    state.error_code = None
                else:
                    state.status = "cancelled"
            else:
                state.status = "unknown"
                state.error_code = "FC_RESULT_UNAVAILABLE"
            if path is not None:
                self._persist_state_unlocked(state, path)
            return self._state_payload(state)

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        validate_request(request)
        if request.timeout_seconds > self.config.max_timeout_seconds:
            raise ExecutionError(
                "FC_COST_CAP_EXCEEDED",
                f"sandbox timeout exceeds the {self.config.max_timeout_seconds}s cost cap",
            )
        if request.network_allowed and not self.config.allow_network:
            raise ExecutionError(
                "FC_NETWORK_DENIED", "network access is disabled by DevPilot policy"
            )
        install_dependencies = self._should_install_dependencies(request)
        install_pytest = self._should_install_pytest(request)
        if install_dependencies and not self.config.allow_network:
            raise ExecutionError(
                "FC_DEPENDENCY_INSTALL_NETWORK_DENIED",
                "project dependency preparation requires an explicitly permitted FC network policy",
            )
        if (
            install_pytest
            and self.config.network_policy_mode == "required"
            and not self.config.allow_network
        ):
            raise ExecutionError(
                "FC_PYTEST_INSTALL_NETWORK_DENIED",
                "pytest bootstrap requires an explicitly permitted FC network policy",
            )
        state = self._reserve_run(request.run_id)
        sandbox_cls = _sandbox_class()
        sandbox: Any | None = None
        started = time.monotonic()
        stage = "create"
        primary_error: ExecutionError | None = None
        effective_network_policy = self.config.network_policy_mode
        try:
            create_kwargs: dict[str, Any] = {
                "template": self.config.template,
                "api_key": self.config.api_key,
                "api_url": self.config.api_url,
                "domain": self.config.domain,
                "timeout": min(
                    self.config.max_timeout_seconds,
                    request.timeout_seconds
                    + (DEPENDENCY_INSTALL_TIMEOUT_SECONDS if install_dependencies else 0)
                    + (PYTEST_INSTALL_TIMEOUT_SECONDS if install_pytest else 0),
                ),
            }
            if self.config.network_policy_mode == "required":
                create_kwargs["allow_internet_access"] = (
                    self.config.allow_network
                    if install_dependencies or install_pytest
                    else self.config.allow_network and request.network_allowed
                )
                if self.config.allow_network and (install_dependencies or install_pytest):
                    effective_network_policy = (
                        "required-with-dependency-bootstrap"
                        if install_dependencies
                        else "required-with-pytest-bootstrap"
                    )
            sandbox_handle: Any = sandbox_cls.create(**create_kwargs)
            if sandbox_handle is None:
                raise ExecutionError("FC_SANDBOX_CREATE_FAILED", "FC Sandbox returned no handle")
            sandbox = sandbox_handle
            with self._lock:
                state.handle = sandbox_handle
                state.sandbox_id = _sandbox_id(sandbox_handle)
                state.status = "running"
                self._persist_state(state)
                if state.cancel_requested:
                    raise ExecutionError("FC_CANCELLED", "FC Sandbox run was cancelled")
            stage = "upload"
            for item in request.files:
                relative_path = item.relative_path.replace("\\", "/")
                target = f"{SANDBOX_ROOT}/{relative_path}"
                parent = target.rsplit("/", 1)[0]
                sandbox_handle.files.make_dir(parent)
                sandbox_handle.files.write(target, item.content)
            with self._lock:
                if state.cancel_requested:
                    raise ExecutionError("FC_CANCELLED", "FC Sandbox run was cancelled")
            if install_dependencies:
                stage = "dependency"
                self._install_dependencies(sandbox_handle, request)
                with self._lock:
                    if state.cancel_requested:
                        raise ExecutionError("FC_CANCELLED", "FC Sandbox run was cancelled")
            if install_pytest:
                stage = "dependency"
                self._install_pytest(sandbox_handle)
                with self._lock:
                    if state.cancel_requested:
                        raise ExecutionError("FC_CANCELLED", "FC Sandbox run was cancelled")
            stage = "execute"
            try:
                try:
                    result = sandbox_handle.commands.run(
                        request.command,
                        timeout=request.timeout_seconds,
                        cwd=self._sandbox_working_directory(request),
                    )
                except TypeError as exc:
                    # Keep compatibility with older SDKs used by local
                    # contract doubles.  Production FC runtimes support cwd;
                    # without it, a project-level test cannot reliably import
                    # the uploaded package root.
                    if "cwd" not in str(exc):
                        raise
                    result = sandbox_handle.commands.run(
                        request.command, timeout=request.timeout_seconds
                    )
            except Exception as exc:
                if not isinstance(getattr(exc, "exit_code", None), int):
                    raise
                result = exc
            with self._lock:
                if state.cancel_requested:
                    raise ExecutionError("FC_CANCELLED", "FC Sandbox run was cancelled")
            raw_stdout = str(getattr(result, "stdout", ""))
            raw_stderr = str(getattr(result, "stderr", ""))
            stdout_bytes = raw_stdout.encode("utf-8")
            stderr_bytes = raw_stderr.encode("utf-8")
            output_truncated = (
                len(stdout_bytes) > request.max_output_bytes
                or len(stderr_bytes) > request.max_output_bytes
            )
            stdout = stdout_bytes[-request.max_output_bytes :].decode("utf-8", errors="replace")
            stderr = stderr_bytes[-request.max_output_bytes :].decode("utf-8", errors="replace")
            raw_exit_code = getattr(result, "exit_code", None)
            if not isinstance(raw_exit_code, int) or isinstance(raw_exit_code, bool):
                raise ExecutionError("FC_RECEIPT_INCOMPLETE", "FC Sandbox receipt has no exit code")
            receipt = ExecutionResult(
                backend=self.name,
                sandbox_id=state.sandbox_id or _sandbox_id(sandbox_handle),
                exit_code=raw_exit_code,
                stdout=stdout,
                stderr=stderr,
                duration_ms=int((time.monotonic() - started) * 1000),
                command_profile=request.command_profile,
                bundle_bytes=execution_bundle_bytes(request),
                region=self.config.region,
                template=self.config.template,
                network_policy=effective_network_policy,
                output_truncated=output_truncated,
                # The adapter creates a short-lived sandbox from the explicit
                # bundle and only admits server-owned verification commands.
                # Even when the provider cannot enforce its optional network
                # flag (``provider-default``), this contract still has no
                # host-project write path and the request itself forbids
                # network use.  Report that bounded execution fact so a
                # complete non-mutating receipt is not discarded solely for
                # lacking an optional provider capability.
                side_effect_count=(
                    0
                    if not request.network_allowed
                    else None
                ),
                permission_violation=(
                    False
                    if not request.network_allowed
                    else None
                ),
                failure_kind="semantic",
            )
            with self._lock:
                state.result = receipt
                state.status = "completed" if receipt.exit_code == 0 else "failed"
                self._persist_state(state)
            return receipt
        except ExecutionError as exc:
            primary_error = exc
            with self._lock:
                state.error_code = exc.code
                state.status = "cancelled" if exc.code == "FC_CANCELLED" else "failed"
                self._persist_state(state)
            raise
        except Exception as exc:
            with self._lock:
                cancelled = state.cancel_requested
            mapped = (
                ExecutionError("FC_CANCELLED", "FC Sandbox run was cancelled")
                if cancelled
                else _provider_error(stage, exc)
            )
            primary_error = mapped
            with self._lock:
                state.error_code = mapped.code
                state.status = "cancelled" if mapped.code == "FC_CANCELLED" else "failed"
                self._persist_state(state)
            raise mapped from exc
        finally:
            with self._lock:
                cleanup_done = state.cleanup_done
            if sandbox is not None and not cleanup_done:
                try:
                    with self._lock:
                        state.cleanup_attempts += 1
                    sandbox.kill()
                    with self._lock:
                        state.cleanup_done = True
                        state.cleanup_error = None
                        self._persist_state(state)
                except Exception as exc:
                    cleanup_error = _provider_error("cleanup", exc)
                    with self._lock:
                        state.cleanup_error = cleanup_error.code
                        cleanup_error.details.update(
                            {
                                "sandbox_id": state.sandbox_id,
                                "cleanup_status": (
                                    "manual_pending"
                                    if state.cleanup_attempts >= MAX_CLEANUP_ATTEMPTS
                                    else "pending"
                                ),
                                "cleanup_error": cleanup_error.code,
                            }
                        )
                        if primary_error is not None:
                            primary_error.details.update(cleanup_error.details)
                        if primary_error is None:
                            state.status = "cleanup_failed"
                            state.error_code = cleanup_error.code
                        self._persist_state(state)
                    if primary_error is None:
                        raise cleanup_error from exc
            with self._lock:
                if state.cleanup_done:
                    state.handle = None
                self._persist_state(state)


def build_fc_backend_from_environment() -> ExecutionBackend:
    """Create the optional backend only when the caller explicitly requests it."""

    return FCSandboxBackend(FCSandboxConfig.from_environment())
