from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient
from nanobot.agent.tools.context import RequestContext, request_context

import devpilot.nanobot_tools as nanobot_tools
from devpilot.code_access import CodeAccessService
from devpilot.control_plane import ControlPlaneManager, create_app
from devpilot.domain import FileOperation
from devpilot.execution import (
    MAX_EXECUTION_TIMEOUT_SECONDS,
    ExecutionError,
    ExecutionRequest,
    ExecutionResult,
    FakeExecutionBackend,
)
from devpilot.nanobot_tools import ProjectTestRunTool
from devpilot.patches import FilePatch, PatchError
from devpilot.paths import AllowedRoots
from devpilot.services import ProjectAnalysisService
from devpilot.storage import ProjectStore
from devpilot.test_runs import ProjectTestRunner
from devpilot.workflows import BugFixWorkflow


def _trusted_store(tmp_path: Path, project: Path) -> tuple[ProjectStore, AllowedRoots]:
    store = ProjectStore(tmp_path / "data" / "devpilot.db")
    store.initialize()
    roots = AllowedRoots.from_paths([tmp_path])
    root_id, fingerprint = roots.binding_for(project)
    project_id = store.ensure_project(
        str(project.resolve()), allowed_root_id=root_id, repo_fingerprint=fingerprint
    )
    store.trust_project(project_id, allowed_root_id=root_id, repo_fingerprint=fingerprint)
    return store, roots


def _untrusted_store(tmp_path: Path, project: Path) -> tuple[ProjectStore, AllowedRoots]:
    store = ProjectStore(tmp_path / "data" / "devpilot.db")
    store.initialize()
    roots = AllowedRoots.from_paths([tmp_path])
    root_id, fingerprint = roots.binding_for(project)
    store.ensure_project(
        str(project.resolve()), allowed_root_id=root_id, repo_fingerprint=fingerprint
    )
    return store, roots


def test_agent_can_create_trusted_failure_run_from_a_project_pytest(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def price():\n    return -100\n", encoding="utf-8")
    (project / "test_main.py").write_text("from main import price\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)
    runner = ProjectTestRunner(
        allowed_roots=roots,
        store=store,
        execution_backend=FakeExecutionBackend(exit_code=1, stdout="1 failed"),
    )

    result = runner.run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="price test fails",
        expected_behavior="price test passes",
    )

    assert result.status == "failed"
    assert result.failure_run_id is not None
    assert result.command == "python -m pytest -q"
    state = store.get_run_state(result.failure_run_id)
    assert state["kind"] == "test-check"
    assert state["status"] == "failed"
    event_types = [event["event_type"] for event in state["events"]]
    assert event_types.index("test.prepare.started") < event_types.index(
        "test.prepare.completed"
    )
    assert "test.execution.completed" in event_types
    assert "test_check.failed" in event_types
    assert result.error_code == "TEST_FAILED"
    assert result.error_category == "test_assertion"
    assert result.retryable is False


def test_execution_failure_has_structured_recovery_metadata(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    class UnavailableBackend:
        name = "unavailable-backend"

        def run(self, _request: object) -> ExecutionResult:
            raise ExecutionError("SANDBOX_UNAVAILABLE", "provider is temporarily unavailable")

    result = ProjectTestRunner(
        allowed_roots=roots,
        store=store,
        execution_backend=UnavailableBackend(),  # type: ignore[arg-type]
    ).run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="test cannot run",
        expected_behavior="test should run",
    )

    assert result.status == "unknown"
    assert result.phase == "execution"
    assert result.error_code == "SANDBOX_UNAVAILABLE"
    assert result.error_category == "sandbox"
    assert result.retryable is False
    assert result.next_action is not None
    assert result.failure_run_id is None


def test_dependency_prepare_failure_is_not_a_test_failure(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "requirements.txt").write_text("does-not-exist==0\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    class DependencyFailureBackend:
        name = "dependency-failure"

        def run(self, _request: object) -> ExecutionResult:
            raise ExecutionError("FC_DEPENDENCY_INSTALL_FAILED", "dependency install failed")

    result = ProjectTestRunner(
        allowed_roots=roots,
        store=store,
        execution_backend=DependencyFailureBackend(),  # type: ignore[arg-type]
    ).run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="test cannot start",
        expected_behavior="test runs",
    )

    assert result.status == "unknown"
    assert result.phase == "prepare"
    assert result.failure_run_id is None
    assert result.error_category == "dependency_prepare"
    events = [event["event_type"] for event in store.list_run_events(result.run_id)]
    assert "test.prepare.failed" in events
    assert "test_check.failed" not in events


def test_transient_project_test_recovery_is_bounded_and_linked(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    class TransientBackend:
        name = "transient-backend"
        verification_available = True

        @staticmethod
        def supports_profile(_profile: str) -> bool:
            return True

        def run(self, _request: object) -> ExecutionResult:
            raise ExecutionError(
                "SANDBOX_UNAVAILABLE",
                "provider is temporarily unavailable",
                details={"safe_to_retry": True},
            )

    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=TransientBackend(),  # type: ignore[arg-type]
    )
    first = manager.run_project_test(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="test cannot start",
        expected_behavior="test runs",
    )
    second = manager.recover_project_test(first["run_id"])
    third = manager.recover_project_test(first["run_id"])
    exhausted = manager.recover_project_test(first["run_id"])

    assert second["recovery_of"] == first["run_id"]
    assert second["retry_count"] == 1
    assert third["recovery_of"] == first["run_id"]
    assert third["retry_count"] == 2
    assert exhausted["error_code"] == "RETRY_EXHAUSTED"
    assert exhausted["retry_count"] == 2
    assert len(store.list_project_run_ids(project_id=first["project_id"], kind="test-check")) == 3
    event_types = [event["event_type"] for event in store.list_run_events(first["run_id"])]
    assert event_types.count("test.recovery.started") == 2
    assert "test.recovery.completed" in event_types


def test_successful_project_test_recovery_cannot_be_replayed(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    class RecoverOnceBackend:
        name = "recover-once-backend"
        verification_available = True

        def __init__(self) -> None:
            self.calls = 0

        @staticmethod
        def supports_profile(_profile: str) -> bool:
            return True

        def run(self, request: object) -> ExecutionResult:
            self.calls += 1
            if self.calls == 1:
                raise ExecutionError(
                    "FC_PROVIDER_UNAVAILABLE",
                    "provider disconnected after safe termination",
                    details={"safe_to_retry": True},
                )
            return ExecutionResult(
                backend=self.name,
                sandbox_id=f"sandbox-{self.calls}",
                exit_code=0,
                stdout="passed",
                stderr="",
                duration_ms=1,
                command_profile=getattr(request, "command_profile", ""),
                side_effect_count=0,
                permission_violation=False,
            )

    backend = RecoverOnceBackend()
    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=backend,  # type: ignore[arg-type]
    )
    first = manager.run_project_test(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="provider disconnected",
        expected_behavior="one safe recovery succeeds",
    )

    recovered = manager.recover_project_test(first["run_id"])
    replayed = manager.recover_project_test(first["run_id"])

    assert recovered["status"] == "passed"
    assert replayed["status"] == "passed"
    assert replayed["already_recovered"] is True
    assert replayed["run_id"] == recovered["run_id"]
    assert backend.calls == 2


@pytest.mark.parametrize("recovery_exit_code", [1, None])
def test_nonretryable_recovery_outcome_cannot_be_replayed(
    tmp_path: Path, recovery_exit_code: int | None
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    class FailedRecoveryBackend:
        name = "failed-recovery-backend"
        verification_available = True

        def __init__(self) -> None:
            self.calls = 0

        @staticmethod
        def supports_profile(_profile: str) -> bool:
            return True

        def run(self, request: object) -> ExecutionResult:
            self.calls += 1
            if self.calls == 1:
                raise ExecutionError(
                    "FC_PROVIDER_UNAVAILABLE",
                    "provider disconnected after safe termination",
                    details={"safe_to_retry": True},
                )
            return ExecutionResult(
                backend=self.name,
                sandbox_id="recovery-sandbox",
                exit_code=recovery_exit_code,  # type: ignore[arg-type]
                stdout="failed" if recovery_exit_code else "",
                stderr="",
                duration_ms=1,
                command_profile=getattr(request, "command_profile", ""),
                side_effect_count=0,
                permission_violation=False,
            )

    backend = FailedRecoveryBackend()
    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=backend,  # type: ignore[arg-type]
    )
    first = manager.run_project_test(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="provider disconnected",
        expected_behavior="do not replay semantic failures or incomplete receipts",
    )

    recovered = manager.recover_project_test(first["run_id"])
    assert recovered["status"] in {"failed", "unknown"}
    with pytest.raises(PatchError, match="PROJECT_TEST_RECOVERY_NOT_RETRYABLE"):
        manager.recover_project_test(first["run_id"])

    assert backend.calls == 2


def test_project_test_provider_error_without_termination_proof_is_not_retryable(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    class UncertainBackend:
        name = "uncertain-backend"
        verification_available = True

        @staticmethod
        def supports_profile(_profile: str) -> bool:
            return True

        def run(self, _request: object) -> ExecutionResult:
            raise ExecutionError(
                "FC_PROVIDER_UNAVAILABLE", "execution outcome is unknown"
            )

    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=UncertainBackend(),  # type: ignore[arg-type]
    )
    result = manager.run_project_test(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="provider disconnected",
        expected_behavior="unknown results are not replayed",
    )

    assert result["retryable"] is False
    with pytest.raises(PatchError, match="PROJECT_TEST_NOT_RETRYABLE"):
        manager.recover_project_test(result["run_id"])


def test_concurrent_project_test_recovery_requests_do_not_start_two_runs(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)
    recovery_started = Event()
    allow_recovery_to_finish = Event()

    class SlowRecoveryBackend:
        name = "slow-recovery-backend"
        verification_available = True

        def __init__(self) -> None:
            self.calls = 0

        @staticmethod
        def supports_profile(_profile: str) -> bool:
            return True

        def run(self, request: object) -> ExecutionResult:
            self.calls += 1
            if self.calls == 1:
                raise ExecutionError(
                    "FC_PROVIDER_UNAVAILABLE",
                    "provider disconnected after safe termination",
                    details={"safe_to_retry": True},
                )
            recovery_started.set()
            assert allow_recovery_to_finish.wait(2)
            return ExecutionResult(
                backend=self.name,
                sandbox_id="recovered-sandbox",
                exit_code=0,
                stdout="passed",
                stderr="",
                duration_ms=1,
                command_profile=getattr(request, "command_profile", ""),
                side_effect_count=0,
                permission_violation=False,
            )

    backend = SlowRecoveryBackend()
    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=backend,  # type: ignore[arg-type]
    )
    first = manager.run_project_test(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="provider disconnected",
        expected_behavior="only one recovery runs at a time",
    )
    recovered: dict[str, object] = {}
    errors: list[Exception] = []

    def recover() -> None:
        try:
            recovered.update(manager.recover_project_test(first["run_id"]))
        except Exception as exc:  # surfaced in the test thread below
            errors.append(exc)

    worker = Thread(target=recover, daemon=True)
    worker.start()
    assert recovery_started.wait(2)
    with pytest.raises(PatchError, match="PROJECT_TEST_RECOVERY_IN_PROGRESS"):
        manager.recover_project_test(first["run_id"])
    assert backend.calls == 2

    allow_recovery_to_finish.set()
    worker.join(2)
    assert not worker.is_alive()
    assert errors == []
    assert recovered["status"] == "passed"
    assert backend.calls == 2


def test_interrupted_recovery_before_run_start_is_reconciled_after_restart(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    class RestartBackend:
        name = "restart-backend"
        verification_available = True

        def __init__(self) -> None:
            self.calls = 0

        @staticmethod
        def supports_profile(_profile: str) -> bool:
            return True

        def run(self, request: object) -> ExecutionResult:
            self.calls += 1
            if self.calls == 1:
                raise ExecutionError(
                    "FC_PROVIDER_UNAVAILABLE",
                    "provider disconnected after safe termination",
                    details={"safe_to_retry": True},
                )
            return ExecutionResult(
                backend=self.name,
                sandbox_id="restart-recovered-sandbox",
                exit_code=0,
                stdout="passed",
                stderr="",
                duration_ms=1,
                command_profile=getattr(request, "command_profile", ""),
                side_effect_count=0,
                permission_violation=False,
            )

    backend = RestartBackend()
    first_manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=backend,  # type: ignore[arg-type]
    )
    first = first_manager.run_project_test(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="provider disconnected",
        expected_behavior="reconcile a recovery interrupted before Run creation",
    )
    intended_run_id = "reserved-before-restart"
    recovery_key = f"project-test-recovery-{first['run_id']}-1"
    reservation_state, _ = store.reserve_idempotency(recovery_key, "pending-request-hash")
    assert reservation_state == "new"
    assert store.get_idempotency(recovery_key) == ("pending-request-hash", None)
    store.append_event(
        first["run_id"],
        "test.recovery.started",
        {
            "attempt": 1,
            "code": "FC_PROVIDER_UNAVAILABLE",
            "retryable": True,
            "recovery_run_id": intended_run_id,
            "idempotency_key": recovery_key,
        },
    )
    restarted_manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=backend,  # type: ignore[arg-type]
    )

    with pytest.raises(PatchError, match="PROJECT_TEST_RECOVERY_INTERRUPTED"):
        restarted_manager.recover_project_test(first["run_id"])
    assert store.get_idempotency(recovery_key) is None
    recovered = restarted_manager.recover_project_test(first["run_id"])

    assert recovered["status"] == "passed"
    assert recovered["retry_count"] == 2
    assert recovered["run_id"] != intended_run_id
    assert backend.calls == 2
    attempts = [
        event
        for event in store.list_run_events(first["run_id"])
        if event["event_type"] == "test.recovery.failed"
    ]
    assert attempts[-1]["payload"]["code"] == "RECOVERY_INTERRUPTED_BEFORE_RUN"
    assert attempts[-1]["payload"]["safe_to_retry"] is True


@pytest.mark.parametrize(
    ("error_code", "message"),
    [
        ("FC_TIMEOUT", "FC Sandbox execute timed out"),
        ("FC_PROVIDER_UNAVAILABLE", "FC Sandbox provider is disconnected"),
    ],
)
def test_injected_fc_provider_fault_enters_bounded_recovery(
    tmp_path: Path, error_code: str, message: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    class FaultInjectedBackend:
        name = "fc-fault-injected"
        verification_available = True

        def __init__(self) -> None:
            self.calls = 0

        @staticmethod
        def supports_profile(_profile: str) -> bool:
            return True

        def run(self, request: object) -> ExecutionResult:
            self.calls += 1
            if self.calls == 1:
                raise ExecutionError(error_code, message, details={"safe_to_retry": True})
            return ExecutionResult(
                backend=self.name,
                sandbox_id=f"sandbox-recovered-{self.calls}",
                exit_code=0,
                stdout="recovered",
                stderr="",
                duration_ms=1,
                command_profile=getattr(request, "command_profile", ""),
                side_effect_count=0,
                permission_violation=False,
            )

    backend = FaultInjectedBackend()
    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=backend,  # type: ignore[arg-type]
    )
    first = manager.run_project_test(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="provider interrupted the test",
        expected_behavior="the test can be retried safely",
    )

    assert first["status"] == "unknown"
    assert first["error_code"] == error_code
    assert first["error_category"] == "sandbox"
    assert first["retryable"] is True
    recovered = manager.recover_project_test(first["run_id"])

    assert recovered["status"] == "passed"
    assert recovered["recovery_of"] == first["run_id"]
    assert recovered["retry_count"] == 1
    assert backend.calls == 2
    parent_events = store.list_run_events(first["run_id"])
    start_event = next(
        event for event in parent_events if event["event_type"] == "test.recovery.started"
    )
    completion_event = next(
        event for event in parent_events if event["event_type"] == "test.recovery.completed"
    )
    assert completion_event["payload"]["recovery_run_id"] == start_event["payload"][
        "recovery_run_id"
    ]


def test_interrupted_running_recovery_is_cleaned_and_not_replayed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)
    cleanup_calls: list[str] = []

    class RestartBackend:
        name = "restart-backend"
        verification_available = True

        def __init__(self) -> None:
            self.run_calls = 0

        @staticmethod
        def supports_profile(_profile: str) -> bool:
            return True

        def run(self, _request: object) -> ExecutionResult:
            self.run_calls += 1
            raise ExecutionError(
                "FC_PROVIDER_UNAVAILABLE",
                "provider disconnected after safe termination",
                details={"safe_to_retry": True},
            )

        def cleanup(self, run_id: str) -> dict[str, str]:
            cleanup_calls.append(run_id)
            return {"status": "cancelled", "cleanup_status": "cleaned"}

    backend = RestartBackend()
    first_manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=backend,  # type: ignore[arg-type]
    )
    first = first_manager.run_project_test(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="provider disconnected",
        expected_behavior="an interrupted remote recovery must be stopped, not replayed",
    )
    interrupted_run_id = "running-recovery-after-restart"
    store.start_run(first["project_id"], kind="test-check", run_id=interrupted_run_id)
    store.append_event(
        first["run_id"],
        "test.recovery.started",
        {
            "attempt": 1,
            "code": "FC_PROVIDER_UNAVAILABLE",
            "retryable": True,
            "recovery_run_id": interrupted_run_id,
            "idempotency_key": f"project-test-recovery-{first['run_id']}-1",
        },
    )
    restarted_manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=backend,  # type: ignore[arg-type]
    )

    with pytest.raises(PatchError, match="PROJECT_TEST_RECOVERY_INTERRUPTED"):
        restarted_manager.recover_project_test(first["run_id"])
    with pytest.raises(PatchError, match="PROJECT_TEST_RECOVERY_NOT_RETRYABLE"):
        restarted_manager.recover_project_test(first["run_id"])

    assert cleanup_calls == [interrupted_run_id]
    assert backend.run_calls == 1
    assert store.get_run_state(interrupted_run_id)["status"] == "failed"
    assert any(
        event["event_type"] == "test.execution.incomplete"
        for event in store.list_run_events(interrupted_run_id)
    )
    last_recovery = [
        event
        for event in store.list_run_events(first["run_id"])
        if event["event_type"] == "test.recovery.failed"
    ][-1]
    assert last_recovery["payload"]["cleanup_status"] == "cleaned"
    assert last_recovery["payload"]["safe_to_retry"] is False


def test_interrupted_recovery_reconciles_completed_run_without_reexecution(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    class RestartBackend:
        name = "restart-backend"
        verification_available = True

        def __init__(self) -> None:
            self.run_calls = 0

        @staticmethod
        def supports_profile(_profile: str) -> bool:
            return True

        def run(self, _request: object) -> ExecutionResult:
            self.run_calls += 1
            raise ExecutionError(
                "FC_PROVIDER_UNAVAILABLE",
                "provider disconnected after safe termination",
                details={"safe_to_retry": True},
            )

    backend = RestartBackend()
    first_manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=backend,  # type: ignore[arg-type]
    )
    first = first_manager.run_project_test(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="provider disconnected",
        expected_behavior="find the completed result after the manager restarts",
    )
    completed_run_id = "completed-recovery-after-restart"
    store.start_run(first["project_id"], kind="test-check", run_id=completed_run_id)
    store.append_event(
        completed_run_id,
        "test.execution.completed",
        {
            "command": "python -m pytest -q",
            "sandbox_id": "completed-sandbox",
            "exit_code": 0,
        },
    )
    store.finish_run(completed_run_id, "completed", {"exit_code": 0})
    store.append_event(
        first["run_id"],
        "test.recovery.started",
        {
            "attempt": 1,
            "code": "FC_PROVIDER_UNAVAILABLE",
            "retryable": True,
            "recovery_run_id": completed_run_id,
            "idempotency_key": f"project-test-recovery-{first['run_id']}-1",
        },
    )
    restarted_manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=backend,  # type: ignore[arg-type]
    )

    recovered = restarted_manager.recover_project_test(first["run_id"])

    assert recovered["run_id"] == completed_run_id
    assert recovered["status"] == "passed"
    assert recovered["recovered_after_restart"] is True
    assert backend.run_calls == 1


def test_missing_pytest_uses_a_dependency_free_plain_runner(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def price():\n    return 80\n", encoding="utf-8")
    tests = project / "tests"
    tests.mkdir()
    (tests / "test_main.py").write_text(
        "from main import price\n\n\ndef test_price():\n    assert price() == 80\n",
        encoding="utf-8",
    )
    store, roots = _trusted_store(tmp_path, project)

    class MissingPytestThenPlainRunner:
        name = "test-backend"

        def __init__(self) -> None:
            self.requests: list[object] = []

        def run(self, request: object) -> ExecutionResult:
            self.requests.append(request)
            if len(self.requests) == 1:
                return ExecutionResult(
                    backend=self.name,
                    sandbox_id="sandbox-pytest",
                    exit_code=1,
                    stdout="",
                    stderr="/workspace/devpilot: No module named pytest",
                    duration_ms=1,
                    command_profile="project-pytest",
                    bundle_bytes=1,
                    output_truncated=False,
                    side_effect_count=0,
                    permission_violation=False,
                )
            return ExecutionResult(
                backend=self.name,
                sandbox_id="sandbox-plain",
                exit_code=0,
                stdout="plain-python-tests: passed",
                stderr="",
                duration_ms=1,
                command_profile="project-pytest",
                bundle_bytes=1,
                output_truncated=False,
                side_effect_count=0,
                permission_violation=False,
            )

    backend = MissingPytestThenPlainRunner()
    result = ProjectTestRunner(
        allowed_roots=roots, store=store, execution_backend=backend  # type: ignore[arg-type]
    ).run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="price test fails",
        expected_behavior="price test passes",
    )

    assert result.status == "passed"
    assert result.command == "python /workspace/devpilot/.devpilot/plain_test_runner.py"
    assert len(backend.requests) == 2
    fallback = backend.requests[1]
    assert isinstance(fallback, ExecutionRequest)
    assert fallback.run_id.endswith("-plain")
    assert any(
        item.relative_path == ".devpilot/plain_test_runner.py"
        for item in fallback.files
    )


def test_project_test_selects_nested_working_directory_and_dependency_profile(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    seed = project / "seed_repo"
    tests = seed / "tests"
    tests.mkdir(parents=True)
    (seed / "main.py").write_text("def price():\n    return 80\n", encoding="utf-8")
    (seed / "requirements.txt").write_text("pytest==8.0.0\n", encoding="utf-8")
    (tests / "test_main.py").write_text(
        "from main import price\n\n\ndef test_price():\n    assert price() == 80\n",
        encoding="utf-8",
    )
    store, roots = _trusted_store(tmp_path, project)
    backend = FakeExecutionBackend(exit_code=0)

    result = ProjectTestRunner(
        allowed_roots=roots,
        store=store,
        execution_backend=backend,
    ).run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["seed_repo/main.py"],
        symptom="price test fails",
        expected_behavior="price test passes",
    )

    assert result.status == "passed"
    assert result.working_directory == "seed_repo"
    assert result.dependency_profile == "python-requirements"
    assert len(result.dependency_fingerprint) == 64
    request = backend.requests[0]
    assert request.working_directory == "seed_repo"
    assert request.dependency_profile == "python-requirements"


def test_local_project_test_uses_the_six_hour_execution_limit(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def price():\n    return 80\n", encoding="utf-8")
    (project / "test_main.py").write_text(
        "from main import price\n\n\ndef test_price():\n    assert price() == 80\n",
        encoding="utf-8",
    )
    store, roots = _trusted_store(tmp_path, project)

    class LocalFakeBackend(FakeExecutionBackend):
        name = "local"

        def run(self, request: ExecutionRequest) -> ExecutionResult:
            receipt = super().run(request)
            return replace(
                receipt,
                backend="local",
                sandbox_id="",
                side_effect_count=None,
                permission_violation=None,
                execution_location="local",
            )

    backend = LocalFakeBackend()
    result = ProjectTestRunner(
        allowed_roots=roots, store=store, execution_backend=backend
    ).run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="the project test needs a long local execution window",
        expected_behavior="the local test runner allows up to six hours",
    )

    assert result.status == "passed"
    assert backend.requests[0].timeout_seconds == MAX_EXECUTION_TIMEOUT_SECONDS


def test_missing_pytest_does_not_turn_unsupported_fixture_into_failure_evidence(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def price():\n    return 80\n", encoding="utf-8")
    tests = project / "tests"
    tests.mkdir()
    (tests / "main_test.py").write_text(
        "def test_price(tmp_path):\n    assert True\n",
        encoding="utf-8",
    )
    store, roots = _trusted_store(tmp_path, project)

    class MissingPytestThenUnsupportedRunner:
        name = "test-backend"

        def __init__(self) -> None:
            self.requests: list[object] = []

        def run(self, request: object) -> ExecutionResult:
            self.requests.append(request)
            if len(self.requests) == 1:
                return ExecutionResult(
                    backend=self.name,
                    sandbox_id="sandbox-pytest",
                    exit_code=1,
                    stdout="",
                    stderr="No module named pytest",
                    duration_ms=1,
                    command_profile="project-pytest",
                    bundle_bytes=1,
                    output_truncated=False,
                    side_effect_count=0,
                    permission_violation=False,
                )
            return ExecutionResult(
                backend=self.name,
                sandbox_id="sandbox-plain",
                exit_code=1,
                stdout="",
                stderr="RuntimeError: unsupported pytest fixture: tests/main_test.py:test_price",
                duration_ms=1,
                command_profile="project-pytest",
                bundle_bytes=1,
                output_truncated=False,
                side_effect_count=0,
                permission_violation=False,
            )

    backend = MissingPytestThenUnsupportedRunner()
    result = ProjectTestRunner(
        allowed_roots=roots, store=store, execution_backend=backend  # type: ignore[arg-type]
    ).run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["tests/main_test.py"],
        symptom="price test fails",
        expected_behavior="price test passes",
    )

    assert result.status == "unknown"
    assert result.failure_run_id is None
    fallback = backend.requests[1]
    assert isinstance(fallback, ExecutionRequest)
    runner = next(
        item for item in fallback.files if item.relative_path == ".devpilot/plain_test_runner.py"
    )
    assert b'rglob("*.py")' in runner.content
    assert b'test_file.name.startswith("test_")' in runner.content
    assert b'test_file.name.endswith("_test.py")' in runner.content
    plain_project = tmp_path / "plain-project"
    plain_tests = plain_project / "tests"
    plain_tests.mkdir(parents=True)
    (plain_tests / "conftest.py").write_text(
        "raise RuntimeError('conftest must not run')\n", encoding="utf-8"
    )
    (plain_project / "test_root.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    runner_path = plain_project / "plain_test_runner.py"
    runner_path.write_bytes(runner.content)
    plain_result = subprocess.run(
        [sys.executable, str(runner_path)],
        cwd=plain_project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert plain_result.returncode == 0, plain_result.stderr
    state = store.get_run_state(result.run_id)
    assert "test.execution.incomplete" in [event["event_type"] for event in state["events"]]
    assert "test_check.failed" not in [event["event_type"] for event in state["events"]]
    assert ProjectTestRunner._fallback_failure_is_unsupported(  # pyright: ignore[reportPrivateUsage]
        ExecutionResult(
            backend="test-backend",
            sandbox_id="sandbox-plain",
            exit_code=1,
            stdout="",
            stderr="ModuleNotFoundError: No module named 'pytest'",
            duration_ms=1,
            command_profile="project-pytest",
            bundle_bytes=1,
            output_truncated=False,
            side_effect_count=0,
            permission_violation=False,
        )
    )


def test_project_test_api_requires_explicit_project_trust(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _untrusted_store(tmp_path, project)
    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=FakeExecutionBackend(exit_code=1),
    )

    with pytest.raises(Exception, match="PROJECT_TRUST_REQUIRED"):
        manager.run_project_test(
            project_path=project,
            test_kind="python-pytest",
            target_files=["main.py"],
            symptom="test fails",
            expected_behavior="test passes",
        )


def test_full_access_can_enroll_project_for_current_session_without_persistent_trust(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (project / "test_main.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    store, roots = _untrusted_store(tmp_path, project)
    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=FakeExecutionBackend(exit_code=0),
    )

    result = manager.run_project_test(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="test cannot run",
        expected_behavior="test passes",
        session_id="websocket:full-access-session",
        session_access_mode="full",
    )

    assert result["status"] == "passed"
    assert result["phase"] == "test"
    assert result["sandbox_id"] == "fake-sandbox"
    security = store.get_project_security(result["project_id"])
    assert security["trust_state"] != "trusted"
    assert (
        "websocket:full-access-session",
        result["project_id"],
    ) not in manager._session_project_bindings  # type: ignore[reportPrivateUsage]

    client = TestClient(
        create_app(manager=manager, internal_auth_token="test-control-plane-token-0123456789"),
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )
    response = client.post(
        "/v1/project-tests",
        headers={
            "Idempotency-Key": "full-access-project-test-1",
            "X-DevPilot-Workspace-Access": "full",
        },
        json={
            "project_path": str(project),
            "session_id": "websocket:full-access-http-session",
            "test_kind": "python-pytest",
            "target_files": ["main.py"],
            "symptom": "test cannot run",
            "expected_behavior": "test passes",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "passed"


def test_project_test_api_returns_a_real_failure_receipt(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def price():\n    return -100\n", encoding="utf-8")
    (project / "test_main.py").write_text("from main import price\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)
    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=FakeExecutionBackend(exit_code=1, stdout="1 failed"),
    )
    client = TestClient(
        create_app(manager=manager, internal_auth_token="test-control-plane-token-0123456789"),
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )

    response = client.post(
        "/v1/project-tests",
        headers={"Idempotency-Key": "project-test-1"},
        json={
            "project_path": str(project),
            "test_kind": "python-pytest",
            "target_files": ["main.py"],
            "symptom": "price test fails",
            "expected_behavior": "price test passes",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["failure_run_id"] == payload["run_id"]
    assert payload["phase"] == "test"
    assert payload["error_code"] == "TEST_FAILED"
    assert payload["error_category"] == "test_assertion"
    assert payload["retryable"] is False
    assert payload["sandbox_id"] == "fake-sandbox"
    assert store.get_run_state(payload["run_id"])["status"] == "failed"
    replay = client.post(
        "/v1/project-tests",
        headers={"Idempotency-Key": "project-test-1"},
        json={
            "project_path": str(project),
            "test_kind": "python-pytest",
            "target_files": ["main.py"],
            "symptom": "price test fails",
            "expected_behavior": "price test passes",
        },
    )
    assert replay.status_code == 201
    assert replay.json()["run_id"] == payload["run_id"]


def test_missing_test_entry_does_not_create_a_failure_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def price():\n    return -100\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)
    starts: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        store,
        "start_run",
        lambda *args, **kwargs: starts.append((*args, *kwargs.values())) or "not-used",
    )

    with pytest.raises(Exception, match="PROJECT_TEST_INVALID"):
        ControlPlaneManager(
            allowed_roots=roots,
            store=store,
            execution_backend=FakeExecutionBackend(exit_code=1),
        ).run_project_test(
            project_path=project,
            test_kind="python-pytest",
            target_files=["main.py"],
            symptom="price test fails",
            expected_behavior="price test passes",
        )
    assert starts == []


def test_agent_can_select_the_allowlisted_node_test_kind(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text(
        '{"scripts":{"test":"node --test"}}\n', encoding="utf-8"
    )
    (project / "index.js").write_text("export const price = -100;\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    result = ProjectTestRunner(
        allowed_roots=roots,
        store=store,
        execution_backend=FakeExecutionBackend(exit_code=1, stdout="1 failing test"),
    ).run(
        project_path=project,
        test_kind="node-test",
        target_files=["index.js"],
        symptom="price test fails",
        expected_behavior="price test passes",
    )

    assert result.command == "npm test"
    assert result.status == "failed"


def test_changed_project_file_cannot_become_failure_evidence(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "main.py"
    source.write_text("def price():\n    return -100\n", encoding="utf-8")
    (project / "test_main.py").write_text("from main import price\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    class ChangingBackend:
        name = "changing-test-backend"

        def run(self, _request: object) -> ExecutionResult:
            source.write_text("def price():\n    return -80\n", encoding="utf-8")
            return ExecutionResult(
                backend=self.name,
                sandbox_id="sandbox-1",
                exit_code=1,
                stdout="1 failed",
                stderr="",
                duration_ms=1,
                command_profile="project-pytest",
                bundle_bytes=1,
                output_truncated=False,
                side_effect_count=0,
                permission_violation=False,
            )

    result = ProjectTestRunner(
        allowed_roots=roots,
        store=store,
        execution_backend=ChangingBackend(),  # type: ignore[arg-type]
    ).run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="price test fails",
        expected_behavior="price test passes",
    )

    assert result.status == "unknown"
    assert result.failure_run_id is None
    events = store.list_run_events(result.run_id)
    assert [event["event_type"] for event in events].count("test_check.failed") == 0
    assert "test.failure_fact.rejected" in [event["event_type"] for event in events]


def test_truncated_test_receipt_cannot_become_failure_evidence(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def price():\n    return -100\n", encoding="utf-8")
    (project / "test_main.py").write_text("from main import price\n", encoding="utf-8")
    store, roots = _trusted_store(tmp_path, project)

    class TruncatedBackend:
        name = "truncated-test-backend"

        def run(self, _request: object) -> ExecutionResult:
            return ExecutionResult(
                backend=self.name,
                sandbox_id="sandbox-1",
                exit_code=1,
                stdout="truncated",
                stderr="",
                duration_ms=1,
                command_profile="project-pytest",
                bundle_bytes=1,
                output_truncated=True,
                side_effect_count=0,
                permission_violation=False,
            )

    result = ProjectTestRunner(
        allowed_roots=roots,
        store=store,
        execution_backend=TruncatedBackend(),  # type: ignore[arg-type]
    ).run(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="price test fails",
        expected_behavior="price test passes",
    )

    assert result.status == "unknown"
    assert result.failure_run_id is None
    assert "test.execution.incomplete" in [
        event["event_type"] for event in store.list_run_events(result.run_id)
    ]


def test_bug_fix_proposal_accepts_a_server_owned_project_test_failure(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "main.py"
    source.write_text("def price():\n    return -100\n", encoding="utf-8")
    (project / "test_main.py").write_text(
        "from main import price\n\n\ndef test_price():\n    assert price() == -80\n",
        encoding="utf-8",
    )
    store, roots = _trusted_store(tmp_path, project)
    analysis = ProjectAnalysisService(allowed_roots=roots, store=store).inspect(project)
    access = CodeAccessService(allowed_roots=roots, store=store)
    access.search(analysis.project_id, "price", evidence_run_id=analysis.run_id)
    access.read(analysis.project_id, "main.py", evidence_run_id=analysis.run_id)
    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=FakeExecutionBackend(exit_code=1, stdout="1 failed"),
        artifacts_root=tmp_path / "data",
    )
    failure = manager.run_project_test(
        project_path=project,
        test_kind="python-pytest",
        target_files=["main.py"],
        symptom="price test fails",
        expected_behavior="price returns the discounted amount",
    )
    before = source.read_text(encoding="utf-8")
    failure_event = [
        event
        for event in store.list_run_events(str(failure["failure_run_id"]))
        if event["event_type"] == "test_check.failed"
    ][0]
    assert failure_event["payload"]["before_hashes"]["main.py"] == hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    proposal = BugFixWorkflow(manager, artifacts_root=tmp_path / "data").propose(
        project_path=project,
        patches=[
            FilePatch(
                "main.py",
                FileOperation.UPDATE,
                before.replace("-100", "-80"),
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
        ],
        analysis_evidence_ids=[analysis.run_id],
        failure_evidence_ids=[str(failure["failure_run_id"])],
        symptom="price test fails",
        expected_behavior="price returns the discounted amount",
        non_goals=["dependency changes"],
        allowed_files=["main.py"],
    )

    assert proposal.preview["failure_evidence_ids"] == (failure["failure_run_id"],)


def test_agent_tool_posts_only_a_structured_project_test_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        nanobot_tools,
        "_post_project_test",
        lambda payload: captured.update(payload) or {"status": "failed", "run_id": "run-1"},
    )

    result = asyncio.run(
        ProjectTestRunTool().execute(
            project_path="E:/workspace/project",
            test_kind="python-pytest",
            target_files=["main.py"],
            symptom="price test fails",
            expected_behavior="price test passes",
        )
    )

    assert "run-1" in str(result)
    assert captured["test_kind"] == "python-pytest"
    assert ProjectTestRunTool().read_only is True


def test_agent_project_test_tool_resolves_relative_path_from_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        nanobot_tools,
        "_post_project_test",
        lambda payload: captured.update(payload) or {"status": "passed", "run_id": "run-2"},
    )

    with request_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            workspace=tmp_path,
        )
    ):
        result = asyncio.run(
            ProjectTestRunTool().execute(
                project_path="loop-engineering-demo-external",
                test_kind="python-pytest",
                target_files=["seed_repo/tests/test_pricing.py"],
                symptom="test fails",
                expected_behavior="test passes",
            )
        )

    assert "run-2" in str(result)
    assert captured["project_path"] == str(tmp_path / "loop-engineering-demo-external")


def test_project_test_request_waits_for_the_fc_execution_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"status":"failed","run_id":"run-1"}'

    def fake_urlopen(_request: object, timeout: int) -> FakeResponse:
        assert timeout == 6 * 60 * 60 + 60
        return FakeResponse()

    monkeypatch.setattr(nanobot_tools, "urlopen", fake_urlopen)
    result = nanobot_tools._post_project_test(
        {
            "project_path": "E:/workspace/project",
            "test_kind": "python-pytest",
            "target_files": ["tests/main_test.py"],
            "symptom": "tests fail",
            "expected_behavior": "tests pass",
        }
    )

    assert result["run_id"] == "run-1"
