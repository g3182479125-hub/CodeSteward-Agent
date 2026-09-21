"""Server-owned project test execution and failure-evidence recording."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .execution import (
    ExecutionBackend,
    ExecutionError,
    ExecutionFile,
    ExecutionRequest,
    ExecutionResult,
)
from .paths import AllowedRoots
from .redaction import redact_execution_output
from .storage import ProjectStore, TestCheckFailureFact
from .verification import (
    MAX_SNAPSHOT_BYTES,
    MAX_SNAPSHOT_FILES,
    is_verification_snapshot_excluded,
    read_verification_snapshot_file,
)

_TEST_COMMANDS = {
    # The FC Code Interpreter image exposes ``python`` reliably, while
    # ``python3`` is not guaranteed to be on its command path.  Invoke
    # pytest through the interpreter selected by the image so a real test
    # failure is distinguishable from a missing-command failure.
    "python-pytest": ("python -m pytest -q", "project-pytest"),
    "node-test": ("npm test", "project-npm-test"),
}
_PLAIN_TEST_RUNNER_PATH = ".devpilot/plain_test_runner.py"
_PLAIN_TEST_RUNNER = b'''"""Minimal dependency-free fallback for plain test functions."""
from pathlib import Path
import inspect
import runpy
import sys

sys.path.insert(0, str(Path.cwd()))
for test_file in sorted(Path(".").rglob("*.py")):
    if not (
        test_file.name.startswith("test_") or test_file.name.endswith("_test.py")
    ):
        continue
    namespace = runpy.run_path(str(test_file))
    for name, value in sorted(namespace.items()):
        if name.startswith("test_") and callable(value):
            if inspect.signature(value).parameters:
                raise RuntimeError(f"unsupported pytest fixture: {test_file}:{name}")
            value()
print("plain-python-tests: passed")
'''


class ProjectTestTrustError(ValueError):
    """Raised when the target has not received an explicit trust binding."""


class ProjectTestEntryError(ValueError):
    """Raised when the server snapshot has no recognized test entry."""


@dataclass(frozen=True, slots=True)
class ProjectTestRunResult:
    status: str
    project_id: str
    run_id: str
    failure_run_id: str | None
    command: str
    exit_code: int | None


class ProjectTestRunner:
    """Run a server-snapshotted project test without accepting model-made facts."""

    def __init__(
        self,
        *,
        allowed_roots: AllowedRoots,
        store: ProjectStore,
        execution_backend: ExecutionBackend,
    ) -> None:
        self.allowed_roots = allowed_roots
        self.store = store
        self.execution_backend = execution_backend

    def run(
        self,
        *,
        project_path: str | Path,
        test_kind: str,
        target_files: list[str] | tuple[str, ...],
        symptom: str,
        expected_behavior: str,
        task_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
    ) -> ProjectTestRunResult:
        command, command_profile = self.command_for(test_kind)
        project = self.allowed_roots.require_project(project_path)
        targets = tuple(self.store.canonical_failure_path(item) for item in target_files)
        if not targets or len(set(targets)) != len(targets):
            raise ValueError("test target files must be unique and non-empty")
        snapshot = self._snapshot(project)
        if not self._has_test_entry(test_kind, snapshot):
            raise ProjectTestEntryError("no supported project test entry was found")
        before_hashes = self._target_hashes(snapshot, targets)
        root_id, fingerprint = self.allowed_roots.binding_for(project)
        project_id = self.store.ensure_project(
            str(project), allowed_root_id=root_id, repo_fingerprint=fingerprint
        )
        try:
            self.store.require_trusted_project(
                project_id, repo_fingerprint=fingerprint, allowed_root_id=root_id
            )
        except ValueError as exc:
            raise ProjectTestTrustError("trusted project is required for project tests") from exc
        run_id = self.store.start_run(project_id, kind="test-check")
        if all(isinstance(value, str) and value for value in (task_id, session_id, user_id)):
            self.store.append_event(
                run_id,
                "run.context.bound",
                {
                    "task_id": task_id,
                    "session_id": session_id,
                    "user_id": user_id,
                    "workspace": str(project),
                },
            )
        request = ExecutionRequest(
            run_id=run_id,
            project_id=project_id,
            files=snapshot,
            command=command,
            timeout_seconds=300,
            network_allowed=False,
            command_profile=command_profile,
        )
        try:
            receipt = self.execution_backend.run(request)
        except ExecutionError as exc:
            self.store.append_event(
                run_id,
                "test.execution.failed",
                {"code": exc.code, "message": exc.message, "command": command},
            )
            self.store.finish_run(run_id, "failed", {"code": exc.code})
            return ProjectTestRunResult("unknown", project_id, run_id, None, command, None)
        except Exception as exc:
            self.store.append_event(
                run_id,
                "test.execution.failed",
                {"code": "EXECUTION_RECEIPT_INVALID", "error_type": type(exc).__name__},
            )
            self.store.finish_run(run_id, "failed", {"code": "EXECUTION_RECEIPT_INVALID"})
            return ProjectTestRunResult("unknown", project_id, run_id, None, command, None)

        fallback_used = False
        if self._pytest_is_missing(receipt):
            fallback_used = True
            fallback_files = (*snapshot, ExecutionFile(_PLAIN_TEST_RUNNER_PATH, _PLAIN_TEST_RUNNER))
            fallback_request = ExecutionRequest(
                # FC persists sandbox ownership by run ID.  The fallback is
                # a second sandbox attempt for the same logical test record.
                run_id=f"{run_id}-plain",
                project_id=project_id,
                files=fallback_files,
                command=f"python /workspace/devpilot/{_PLAIN_TEST_RUNNER_PATH}",
                timeout_seconds=300,
                network_allowed=False,
                command_profile=command_profile,
            )
            receipt = self.execution_backend.run(fallback_request)
            command = fallback_request.command

        if not isinstance(receipt, ExecutionResult) or not self._receipt_is_complete(
            receipt, command_profile
        ):
            self.store.append_event(
                run_id,
                "test.execution.incomplete",
                {"code": "TEST_RECEIPT_INCOMPLETE", "command": command},
            )
            self.store.finish_run(run_id, "failed", {"code": "TEST_RECEIPT_INCOMPLETE"})
            return ProjectTestRunResult("unknown", project_id, run_id, None, command, None)
        self._record_receipt(run_id, receipt, command)
        if fallback_used and self._fallback_failure_is_unsupported(receipt):
            self.store.append_event(
                run_id,
                "test.execution.incomplete",
                {"code": "TEST_FALLBACK_UNSUPPORTED", "command": command},
            )
            self.store.finish_run(run_id, "failed", {"code": "TEST_FALLBACK_UNSUPPORTED"})
            return ProjectTestRunResult("unknown", project_id, run_id, None, command, None)
        if receipt.exit_code == 0:
            self.store.finish_run(run_id, "completed", {"exit_code": 0})
            return ProjectTestRunResult("passed", project_id, run_id, None, command, 0)
        try:
            self.store.record_test_check_failure_fact(
                TestCheckFailureFact._from_server(  # pyright: ignore[reportPrivateUsage]
                    run_id=run_id,
                    project_id=project_id,
                    symptom=symptom,
                    expected_behavior=expected_behavior,
                    target_files=targets,
                    before_hashes=before_hashes,
                    code="TEST_FAILED",
                )
            )
        except ValueError as exc:
            self.store.append_event(
                run_id,
                "test.failure_fact.rejected",
                {"reason": str(exc), "command": command},
            )
            self.store.finish_run(run_id, "failed", {"code": "TEST_EVIDENCE_CHANGED"})
            return ProjectTestRunResult(
                "unknown", project_id, run_id, None, command, receipt.exit_code
            )
        self.store.finish_run(run_id, "failed", {"exit_code": receipt.exit_code})
        return ProjectTestRunResult(
            "failed", project_id, run_id, run_id, command, receipt.exit_code
        )

    @staticmethod
    def command_for(test_kind: str) -> tuple[str, str]:
        """Return the server-owned command and backend profile for one test kind."""

        try:
            return _TEST_COMMANDS[test_kind]
        except KeyError as exc:
            raise ValueError("unsupported project test kind") from exc

    @staticmethod
    def _pytest_is_missing(receipt: ExecutionResult) -> bool:
        return receipt.exit_code != 0 and "no module named pytest" in receipt.stderr.casefold()

    @staticmethod
    def _fallback_failure_is_unsupported(receipt: ExecutionResult) -> bool:
        stderr = receipt.stderr.casefold()
        return (
            "unsupported pytest fixture:" in stderr
            or "no module named pytest" in stderr
            or "no module named 'pytest'" in stderr
            or ("modulenotfounderror" in stderr and "pytest" in stderr)
        )

    @staticmethod
    def _target_hashes(
        snapshot: tuple[ExecutionFile, ...], targets: tuple[str, ...]
    ) -> dict[str, str]:
        snapshot_by_path = {item.relative_path: item.content for item in snapshot}
        try:
            return {
                relative_path: hashlib.sha256(snapshot_by_path[relative_path]).hexdigest()
                for relative_path in targets
            }
        except KeyError as exc:
            raise ValueError("test targets must be regular project files in the snapshot") from exc

    @staticmethod
    def _has_test_entry(test_kind: str, snapshot: tuple[ExecutionFile, ...]) -> bool:
        files = {item.relative_path: item.content for item in snapshot}
        if test_kind == "python-pytest":
            return any(
                path.rsplit("/", 1)[-1].startswith("test_") and path.endswith(".py")
                or path.endswith("_test.py")
                for path in files
            )
        package = files.get("package.json")
        if package is None:
            return False
        try:
            parsed = json.loads(package.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        scripts = parsed.get("scripts") if isinstance(parsed, dict) else None
        return isinstance(scripts, dict) and isinstance(scripts.get("test"), str) and bool(
            scripts["test"].strip()
        )

    @staticmethod
    def _receipt_is_complete(receipt: ExecutionResult, command_profile: str) -> bool:
        return bool(
            isinstance(receipt.exit_code, int)
            and not isinstance(receipt.exit_code, bool)
            and receipt.sandbox_id
            and receipt.command_profile == command_profile
            and isinstance(receipt.duration_ms, int)
            and receipt.duration_ms >= 0
            and receipt.side_effect_count == 0
            and receipt.permission_violation is False
            and not receipt.output_truncated
        )

    @staticmethod
    def _snapshot(project: Path) -> tuple[ExecutionFile, ...]:
        files: list[ExecutionFile] = []
        for root, directories, names in os.walk(project, topdown=True, followlinks=False):
            directories[:] = sorted(
                name
                for name in directories
                if not is_verification_snapshot_excluded(
                    str(Path(root, name).relative_to(project))
                )
                and not Path(root, name).is_symlink()
            )
            for name in sorted(names):
                path = Path(root, name)
                if path.is_symlink():
                    continue
                relative_path = path.relative_to(project).as_posix()
                if is_verification_snapshot_excluded(relative_path):
                    continue
                content = read_verification_snapshot_file(path, relative_path)
                if content is not None:
                    files.append(ExecutionFile(relative_path, content))
        if not files:
            raise ValueError("project test snapshot is empty")
        if len(files) > MAX_SNAPSHOT_FILES:
            raise ValueError("project test snapshot exceeds file limit")
        if sum(len(item.content) for item in files) > MAX_SNAPSHOT_BYTES:
            raise ValueError("project test snapshot exceeds byte limit")
        return tuple(files)

    def _record_receipt(self, run_id: str, receipt: ExecutionResult, command: str) -> None:
        self.store.append_event(
            run_id,
            "test.execution.completed",
            {
                "command": command,
                "backend": receipt.backend,
                "sandbox_id": receipt.sandbox_id,
                "command_profile": receipt.command_profile,
                "exit_code": receipt.exit_code,
                "duration_ms": receipt.duration_ms,
                "stdout": redact_execution_output(receipt.stdout),
                "stderr": redact_execution_output(receipt.stderr),
                "output_truncated": receipt.output_truncated,
            },
        )
