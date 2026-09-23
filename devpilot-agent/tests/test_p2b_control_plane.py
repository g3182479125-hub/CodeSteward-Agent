from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from devpilot.control_plane import (
    ControlPlaneManager,
    _candidate_trial_runner_from_environment,
    create_app,
)
from devpilot.domain import FileOperation
from devpilot.execution import ExecutionRequest, ExecutionResult, FakeExecutionBackend
from devpilot.fc_sandbox import FCSandboxBackend, FCSandboxConfig
from devpilot.local_execution import LocalExecutionBackend
from devpilot.nanobot_tools import (
    ExecuteChangeSetTool,
    ProposeChangeSetTool,
    ToolResult,
    VerifyChangeSetTool,
)
from devpilot.patches import FilePatch, PatchError
from devpilot.paths import AllowedRoots
from devpilot.services import BugCheckService
from devpilot.skill_improvement import SkillImprovementStore, task_snapshot_ref
from devpilot.storage import ProjectStore
from devpilot.verification import VerificationProfile


def make_manager(
    tmp_path: Path, execution_backend: object | None = None
) -> tuple[ControlPlaneManager, Path]:
    project = tmp_path / "project"
    project.mkdir()
    store = ProjectStore(tmp_path / "data" / "devpilot.db")
    store.initialize()
    roots = AllowedRoots.from_paths([tmp_path])
    root_id, fingerprint = roots.binding_for(project)
    project_id = store.ensure_project(
        str(project), allowed_root_id=root_id, repo_fingerprint=fingerprint
    )
    store.trust_project(project_id, allowed_root_id=root_id, repo_fingerprint=fingerprint)
    return (
        ControlPlaneManager(
            allowed_roots=roots,
            store=store,
            execution_backend=execution_backend,  # type: ignore[arg-type]
        ),
        project,
    )


def test_control_plane_can_verify_applied_changeset_in_backend(tmp_path: Path) -> None:
    backend = LocalExecutionBackend(state_dir=tmp_path / "local-execution")
    manager, project = make_manager(tmp_path, execution_backend=backend)
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('ok')\n")],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))

    verify_approval = manager.approve(change_set_id, "r2.verify")
    verified = manager.verify(
        change_set_id,
        str(verify_approval["approval_token"]),
        profile=VerificationProfile.PYTHON_SYNTAX,
    )

    assert verified["status"] == "verified"
    assert verified["execution_location"] == "local"
    assert verified["backend"] == "local"
    assert verified["sandbox_id"] == ""
    project_id = str(manager.store.list_projects()[0]["project_id"])
    run_id = manager.store.list_project_run_ids(project_id=project_id, kind="code-delivery")[0]
    verification_event = next(
        event
        for event in manager.store.list_run_events(run_id)
        if event["event_type"] == "changeset.verification_completed"
    )
    assert verification_event["payload"]["execution_location"] == "local"

    rollback_approval = manager.approve(change_set_id, "r1.rollback")
    reverted = manager.rollback(change_set_id, str(rollback_approval["approval_token"]))
    assert reverted["status"] == "reverted"


def test_verify_rejects_receipt_with_inconsistent_execution_location(
    tmp_path: Path,
) -> None:
    class InconsistentReceiptBackend(FakeExecutionBackend):
        verification_available = True

        def supports_profile(self, profile: str) -> bool:
            return True

        def run(self, request: ExecutionRequest) -> ExecutionResult:
            result = super().run(request)
            return dataclasses.replace(result, execution_location="local")

    manager, project = make_manager(tmp_path, execution_backend=InconsistentReceiptBackend())
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('ok')\n")],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))
    verify_approval = manager.approve(change_set_id, "r2.verify")

    with pytest.raises(PatchError, match="verification failed"):
        manager.verify(
            change_set_id,
            str(verify_approval["approval_token"]),
            profile=VerificationProfile.PYTHON_SYNTAX,
        )

    persisted = manager.store.get_change_set(change_set_id)
    assert persisted is not None
    assert persisted["status"] == "verification_failed"
    project_id = str(manager.store.list_projects()[0]["project_id"])
    run_id = manager.store.list_project_run_ids(project_id=project_id, kind="code-delivery")[0]
    failure_event = next(
        event
        for event in manager.store.list_run_events(run_id)
        if event["event_type"] == "changeset.verification_failed"
    )
    assert failure_event["payload"]["code"] == "EXECUTION_RECEIPT_INVALID"


def test_preview_validation_failure_finishes_started_run(tmp_path: Path) -> None:
    manager, project = make_manager(tmp_path)
    (project / "main.py").write_text("print('ok')\n", encoding="utf-8")

    with pytest.raises(PatchError, match="target changed since planning"):
        manager.preview(
            project_path=project,
            project_id=None,
            patches=[
                FilePatch(
                    "main.py",
                    FileOperation.UPDATE,
                    "print('changed')\n",
                    expected_sha256="0" * 64,
                )
            ],
        )

    project_id = manager.store.list_projects()[0]["project_id"]  # type: ignore[union-attr]
    run_id = manager.store.list_project_run_ids(  # type: ignore[union-attr]
        project_id=project_id, kind="code-delivery"
    )[0]
    assert manager.store.get_run_status(run_id) == "failed"  # type: ignore[union-attr]
    events = manager.store.list_run_events(run_id)  # type: ignore[union-attr]
    assert events[-1]["event_type"] == "run.failed"
    assert events[-1]["payload"]["code"] == "PRECONDITION_HASH_MISMATCH"


def test_full_access_changeset_still_requires_approval_before_apply(tmp_path: Path) -> None:
    backend = FakeExecutionBackend(stdout="syntax ok")
    manager, project = make_manager(tmp_path, execution_backend=backend)
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('ok')\n")],
    )

    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    applied = manager.apply(
        change_set_id,
        str(apply_approval["approval_token"]),
        idempotency_key="confirmed-apply-test",
    )
    verify_approval = manager.approve(change_set_id, "r2.verify")
    result = manager.verify(
        change_set_id,
        str(verify_approval["approval_token"]),
        profile=VerificationProfile.PYTHON_SYNTAX,
        idempotency_key="confirmed-verify-test",
    )

    assert applied["status"] == "applied"
    assert result["status"] == "verified"
    assert (project / "main.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert backend.requests[0].command.startswith("python3 -m py_compile")


def test_full_access_failed_verification_records_feature_case_from_server_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeExecutionBackend(exit_code=1, stdout="syntax failed")
    manager, project = make_manager(tmp_path, execution_backend=backend)
    skill_path = project / "skills" / "feature-delivery" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: feature-delivery\n---\n", encoding="utf-8")
    monkeypatch.setenv("DEVPILOT_ENABLE_SKILL_IMPROVEMENT", "1")
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(tmp_path / "data"))
    with sqlite3.connect(tmp_path / "data" / "devpilot.db") as connection:
        connection.execute("create table alembic_version (version_num varchar(32) not null)")
        connection.execute(
            "insert into alembic_version values (?)",
            ("0011_p8_skill_improvement_records",),
        )
        connection.execute(
            "create table skill_improvement_records ("
            "id integer primary key autoincrement, record_key text not null unique, "
            "record_type text not null, record_id text not null, payload_json text not null, "
            "record_mac text not null, workspace text, project_id text, created_at text not null)"
        )
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('broken')\n")],
    )
    change_set_id = str(preview["change_set_id"])
    change_set = manager.store.get_change_set(change_set_id)
    assert change_set is not None
    task_id = str(change_set["task_id"])
    project_id = str(change_set["project_id"])
    run_id = str(change_set["run_id"])
    improvement_store = SkillImprovementStore(tmp_path / "data", workspace=project)
    snapshot = improvement_store.capture_task_snapshot(
        task_id=task_id,
        workspace=project,
        session_id="session-feature",
        project_id=project_id,
        skill_id="feature-delivery",
    )
    manager.store.append_event(
        run_id,
        "feature_delivery.proposed",
        {"change_set_id": change_set_id, "workflow": "feature-delivery"},
    )

    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))
    verify_approval = manager.approve(change_set_id, "r2.verify")
    result = manager.verify(
        change_set_id,
        str(verify_approval["approval_token"]),
        profile=VerificationProfile.PYTHON_SYNTAX,
        skill_improvement_context={
            "skill_id": "feature-delivery",
            "profile_binding": "feature-delivery@2",
            "task_id": task_id,
            "user_id": "user-feature",
            "session_id": "session-feature",
            "skill_hash": snapshot.content_hash,
            "summary": "feature verification failed",
            "expected_behavior": "python-syntax verification passes",
            "verification_kind": "semantic",
            "replay_snapshot_ref": task_snapshot_ref(task_id),
        },
    )

    assert result["status"] == "verification_failed"
    cases = improvement_store.list_improvement_cases(
        workspace=project, user_id="user-feature", project_id=project_id
    )
    assert len(cases) == 1
    assert cases[0].origin_run_id == run_id
    with sqlite3.connect(tmp_path / "data" / "devpilot.db") as connection:
        event = connection.execute(
            "select id, payload_json from run_events "
            "where run_id = ? and event_type = 'changeset.verification_failed'",
            (run_id,),
        ).fetchone()
        assert event is not None
        payload = json.loads(str(event[1]))
        payload["failure_kind"] = "unknown"
        connection.execute(
            "update run_events set payload_json = ? where id = ?",
            (json.dumps(payload), event[0]),
        )
    assert not improvement_store._has_trusted_verification_fact(  # pyright: ignore[reportPrivateUsage]
        verification_ref=f"verification-{run_id}:{change_set_id}",
        origin_run_id=run_id,
        change_set_id=change_set_id,
        project_id=project_id,
        skill_id="feature-delivery",
        task_id=task_id,
        workspace=str(project.resolve()),
    )


def test_full_access_failed_bug_verification_records_case_with_failure_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeExecutionBackend(exit_code=1, stdout="bug still fails")
    manager, project = make_manager(tmp_path, execution_backend=backend)
    target = project / "main.py"
    target.write_text("def price():\n    return -100\n", encoding="utf-8")
    task_id = "task-bug-failure"
    session_id = "session-bug-failure"
    user_id = "user-bug-failure"
    failure = BugCheckService(
        allowed_roots=manager.patch_service.allowed_roots,
        store=manager.store,  # type: ignore[arg-type]
    ).record_failure(
        project,
        symptom="price is negative",
        expected_behavior="price returns a positive value",
        target_files=["main.py"],
        code="BUG_EXPECTATION_FAILED",
        task_id=task_id,
        session_id=session_id,
        user_id=user_id,
    )
    manager.store.finish_run(failure.run_id, "failed", {"code": "BUG_EXPECTATION_FAILED"})
    failure_ref = f"failure-{failure.run_id}"
    monkeypatch.setenv("DEVPILOT_ENABLE_SKILL_IMPROVEMENT", "1")
    monkeypatch.setenv("DEVPILOT_DATA_DIR", str(tmp_path / "data"))
    with sqlite3.connect(tmp_path / "data" / "devpilot.db") as connection:
        connection.execute("create table alembic_version (version_num varchar(32) not null)")
        connection.execute(
            "insert into alembic_version values (?)",
            ("0011_p8_skill_improvement_records",),
        )
        connection.execute(
            "create table skill_improvement_records ("
            "id integer primary key autoincrement, record_key text not null unique, "
            "record_type text not null, record_id text not null, payload_json text not null, "
            "record_mac text not null, workspace text, project_id text, created_at text not null)"
        )
    skill_path = project / "skills" / "bug-fix" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("---\nname: bug-fix\n---\n", encoding="utf-8")
    improvement_store = SkillImprovementStore(tmp_path / "data", workspace=project)
    change = FilePatch(
        "main.py",
        FileOperation.UPDATE,
        "def price():\n    return 100\n",
        expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
    )
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[change],
        task_id=task_id,
    )
    change_set_id = str(preview["change_set_id"])
    change_set = manager.store.get_change_set(change_set_id)
    assert change_set is not None
    project_id = str(change_set["project_id"])
    origin_run_id = str(change_set["run_id"])
    snapshot = improvement_store.capture_task_snapshot(
        task_id=task_id,
        workspace=project,
        session_id=session_id,
        project_id=project_id,
        skill_id="bug-fix",
    )
    manager.store.append_event(
        origin_run_id,
        "bug_fix.proposed",
        {"change_set_id": change_set_id, "failure_evidence_ids": [failure_ref]},
    )

    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))
    verify_approval = manager.approve(change_set_id, "r2.verify")
    result = manager.verify(
        change_set_id,
        str(verify_approval["approval_token"]),
        profile=VerificationProfile.PYTHON_SYNTAX,
        skill_improvement_context={
            "skill_id": "bug-fix",
            "profile_binding": "bug-fix@1",
            "task_id": task_id,
            "user_id": user_id,
            "session_id": session_id,
            "skill_hash": snapshot.content_hash,
            "summary": "bug verification failed",
            "expected_behavior": "price returns a positive value",
            "verification_kind": "semantic",
            "failure_evidence_refs": [failure_ref],
            "replay_snapshot_ref": task_snapshot_ref(task_id),
        },
    )

    assert result["status"] == "verification_failed"
    cases = improvement_store.list_improvement_cases(
        workspace=project,
        user_id=user_id,
        project_id=project_id,
    )
    assert len(cases) == 1
    assert cases[0].skill_id == "bug-fix"
    assert cases[0].origin_run_id == origin_run_id


def test_unknown_backend_failure_does_not_enter_skill_improvement(
    tmp_path: Path,
) -> None:
    class UnknownFailureBackend(FakeExecutionBackend):
        verification_available = True

        def supports_profile(self, _profile):
            return True

        def run(self, request):
            return dataclasses.replace(
                super().run(request),
                failure_kind="unknown",
            )

    manager, project = make_manager(
        tmp_path,
        execution_backend=UnknownFailureBackend(exit_code=1),
    )
    recorded: list[object] = []
    manager._record_skill_improvement_failure = (  # type: ignore[method-assign]
        lambda entry, context: recorded.append((entry, context))
    )
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "broken()\n")],
        task_id="task-unknown-failure",
    )

    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))
    verify_approval = manager.approve(change_set_id, "r2.verify")
    result = manager.verify(
        change_set_id,
        str(verify_approval["approval_token"]),
        profile=VerificationProfile.PYTHON_SYNTAX,
        skill_improvement_context={
            "skill_id": "feature-delivery",
            "verification_kind": "semantic",
        },
    )

    assert result["status"] == "verification_failed"
    assert recorded == []


def test_full_access_direct_apply_verify_endpoint_is_removed(tmp_path: Path) -> None:
    manager, project = make_manager(tmp_path, execution_backend=FakeExecutionBackend())
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('ok')\n")],
    )
    change_set_id = str(preview["change_set_id"])
    app = create_app(
        manager=manager,
        internal_auth_token="control-plane-token-012345678901234567890123",
    )

    from fastapi.testclient import TestClient

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        response = client.post(
            f"/v1/change-sets/{change_set_id}/full-access-apply-verify",
            json={"profile": "python-syntax"},
            headers={
                "Idempotency-Key": "full-access-http-test",
                "X-DevPilot-Internal-Token": "control-plane-token-012345678901234567890123",
            },
        )

    assert response.status_code == 404


def test_control_plane_can_run_python_self_test_in_backend(tmp_path: Path) -> None:
    backend = FakeExecutionBackend(stdout="all self-tests passed")
    manager, project = make_manager(tmp_path, execution_backend=backend)
    content = "assert 2 + 2 == 4\nprint('all self-tests passed')\n"
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, content)],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))

    verify_approval = manager.approve(change_set_id, "r2.verify")
    verified = manager.verify(
        change_set_id,
        str(verify_approval["approval_token"]),
        profile=VerificationProfile.PYTHON_RUN,
    )

    assert verified["status"] == "verified"
    assert backend.requests[0].command == "python3 /workspace/devpilot/main.py"
    assert backend.requests[0].network_allowed is False
    assert backend.requests[0].files[0].content == content.encode("utf-8")


def test_control_plane_routes_python_regression_to_fc_pytest_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, project = make_manager(tmp_path)
    (project / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests" / "test_main.py").write_text(
        "def test_main():\n    assert True\n", encoding="utf-8"
    )
    events: list[tuple[object, ...]] = []

    class FakeFiles:
        def make_dir(self, path: str) -> None:
            events.append(("mkdir", path))

        def write(self, path: str, content: bytes) -> None:
            events.append(("write", path, content))

    class FakeCommands:
        def run(self, command: str, *, timeout: int, cwd: str | None = None) -> SimpleNamespace:
            events.append(("run", command, timeout, cwd))
            return SimpleNamespace(exit_code=0, stdout="5 passed", stderr="")

    class FakeSandbox:
        sandbox_id = "control-plane-regression-sandbox"
        files = FakeFiles()
        commands = FakeCommands()

        def kill(self) -> None:
            events.append(("kill",))

    class Factory:
        @classmethod
        def create(cls, **_kwargs: object) -> FakeSandbox:
            return FakeSandbox()

    monkeypatch.setitem(sys.modules, "e2b_code_interpreter", SimpleNamespace(Sandbox=Factory))
    backend = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
            network_policy_mode="provider-default",
            allowed_project_paths=(str(project),),
            state_dir=str(tmp_path / "fc-state"),
        )
    )
    manager.execution_backend = backend
    expected_sha = hashlib.sha256((project / "main.py").read_bytes()).hexdigest()
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[
            FilePatch(
                "main.py",
                FileOperation.UPDATE,
                "VALUE = 2\n",
                expected_sha,
            )
        ],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))

    verify_approval = manager.approve(change_set_id, "r2.verify")
    verified = manager.verify(
        change_set_id,
        str(verify_approval["approval_token"]),
        profile=VerificationProfile.PYTHON_REGRESSION,
    )

    assert verified["status"] == "verified"
    assert verified["command_profile"] == "python-regression"
    assert [event[:2] for event in events if event[0] == "run"] == [
        ("run", "python -m pip install --disable-pip-version-check pytest"),
        ("run", "python -m pytest -q"),
    ]


def test_control_plane_fail_closed_for_non_fake_backend_without_p6_gate(tmp_path: Path) -> None:
    class RemoteBackend:
        name = "fc-agent-sandbox"

        def run(self, _request: object) -> object:
            raise AssertionError("remote backend must not start before P6 gate")

    manager, project = make_manager(tmp_path, execution_backend=RemoteBackend())
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('ok')\n")],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))
    verify_approval = manager.approve(change_set_id, "r2.verify")

    with pytest.raises(PatchError, match="EXECUTION_BACKEND_UNAVAILABLE"):
        manager.verify(change_set_id, str(verify_approval["approval_token"]))


def test_control_plane_allows_explicitly_available_remote_backend(tmp_path: Path) -> None:
    class AvailableBackend:
        name = "remote-test-double"
        verification_available = True

        def __init__(self) -> None:
            self.delegate = FakeExecutionBackend(stdout="remote verification ok")

        def run(self, request: object) -> object:
            return self.delegate.run(request)  # type: ignore[arg-type]

        def supports_profile(self, profile: str) -> bool:
            return profile == "python-syntax"

        def project_allowed(self, _project_path: str) -> bool:
            return True

    manager, project = make_manager(tmp_path, execution_backend=AvailableBackend())
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('ok')\n")],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))
    verify_approval = manager.approve(change_set_id, "r2.verify")

    result = manager.verify(change_set_id, str(verify_approval["approval_token"]))

    assert result["status"] == "verified"


def test_control_plane_remote_backend_rejects_unproven_profile(tmp_path: Path) -> None:
    class ProfileBoundBackend:
        name = "fc-agent-sandbox"
        verification_available = True

        def supports_profile(self, profile: str) -> bool:
            return profile in {"python-syntax", "node-syntax"}

        def project_allowed(self, _project_path: str) -> bool:
            return True

        def run(self, _request: object) -> object:
            raise AssertionError("react-static must be rejected before provider execution")

    manager, project = make_manager(tmp_path, execution_backend=ProfileBoundBackend())
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[
            FilePatch("Form.jsx", FileOperation.CREATE, "export default function Form() {}\n")
        ],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))
    verify_approval = manager.approve(change_set_id, "r2.verify")

    with pytest.raises(PatchError, match="VERIFICATION_PROFILE_UNSUPPORTED"):
        manager.verify(
            change_set_id,
            str(verify_approval["approval_token"]),
            profile=VerificationProfile.REACT_STATIC,
        )


def test_control_plane_remote_backend_requires_project_allowlist(tmp_path: Path) -> None:
    class AllowlistBackend:
        name = "fc-agent-sandbox"
        verification_available = True

        def supports_profile(self, _profile: str) -> bool:
            return True

        def project_allowed(self, _project_path: str) -> bool:
            return False

        def run(self, _request: object) -> object:
            raise AssertionError("non-allowlisted project must not reach provider")

    manager, project = make_manager(tmp_path, execution_backend=AllowlistBackend())
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('ok')\n")],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))
    verify_approval = manager.approve(change_set_id, "r2.verify")

    with pytest.raises(PatchError, match="EXECUTION_PROJECT_NOT_ALLOWLISTED"):
        manager.verify(change_set_id, str(verify_approval["approval_token"]))


def test_python_run_rejects_multiple_python_files(tmp_path: Path) -> None:
    manager, project = make_manager(tmp_path, execution_backend=FakeExecutionBackend())
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[
            FilePatch("main.py", FileOperation.CREATE, "print('main')\n"),
            FilePatch("helper.py", FileOperation.CREATE, "print('helper')\n"),
        ],
    )
    change_set_id = str(preview["change_set_id"])
    apply_approval = manager.approve(change_set_id)
    manager.apply(change_set_id, str(apply_approval["approval_token"]))
    verify_approval = manager.approve(change_set_id, "r2.verify")

    with pytest.raises(PatchError, match="VERIFICATION_PROFILE_UNSUPPORTED"):
        manager.verify(
            change_set_id,
            str(verify_approval["approval_token"]),
            profile=VerificationProfile.PYTHON_RUN,
        )


def test_control_plane_manager_preview_approve_apply_and_rollback(tmp_path: Path) -> None:
    manager, project = make_manager(tmp_path)
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("src/main.py", FileOperation.CREATE, "print('ok')\n")],
    )

    assert preview["status"] == "planned"
    assert "+++ b/src/main.py" in preview["diff"]
    change_set_id = str(preview["change_set_id"])

    approval = manager.approve(change_set_id)
    applied = manager.apply(change_set_id, str(approval["approval_token"]))
    assert applied["status"] == "applied"
    assert (project / "src" / "main.py").read_text(encoding="utf-8") == "print('ok')\n"

    rollback_approval = manager.approve(change_set_id, "r1.rollback")
    reverted = manager.rollback(change_set_id, str(rollback_approval["approval_token"]))
    assert reverted["status"] == "reverted"
    assert not (project / "src" / "main.py").exists()


def test_ephemeral_manager_fails_closed_before_r1_or_r2_operation(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager = ControlPlaneManager(allowed_roots=AllowedRoots.from_paths([tmp_path]))
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "print('ok')\n")],
    )
    change_set_id = str(preview["change_set_id"])

    with pytest.raises(PatchError, match="PERSISTENCE_REQUIRED"):
        manager.request_approval(change_set_id, "r1.apply", "执行")
    with pytest.raises(PatchError, match="PERSISTENCE_REQUIRED"):
        manager.approve(change_set_id)
    with pytest.raises(PatchError, match="PERSISTENCE_REQUIRED"):
        manager.apply(change_set_id, "unused", idempotency_key="apply-key")
    with pytest.raises(PatchError, match="PERSISTENCE_REQUIRED"):
        manager.verify(change_set_id, "unused", idempotency_key="verify-key")
    with pytest.raises(PatchError, match="PERSISTENCE_REQUIRED"):
        manager.rollback(change_set_id, "unused", idempotency_key="rollback-key")
    assert not (project / "main.py").exists()


def test_control_plane_requires_approval_and_consumes_token(tmp_path: Path) -> None:
    manager, project = make_manager(tmp_path)
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[FilePatch("main.py", FileOperation.CREATE, "new\n")],
    )
    change_set_id = str(preview["change_set_id"])

    with pytest.raises(PatchError, match="APPROVAL_INVALID"):
        manager.apply(change_set_id, "missing")

    token = str(manager.approve(change_set_id)["approval_token"])
    manager.apply(change_set_id, token)
    with pytest.raises(PatchError, match="APPROVAL_INVALID"):
        manager.apply(change_set_id, token)


def test_control_plane_stale_plan_has_zero_controlled_writes(tmp_path: Path) -> None:
    manager, project = make_manager(tmp_path)
    target = project / "main.py"
    target.write_text("old\n", encoding="utf-8", newline="")
    preview = manager.preview(
        project_path=project,
        project_id=None,
        patches=[
            FilePatch(
                "main.py",
                FileOperation.UPDATE,
                "new\n",
                hashlib.sha256(b"old\n").hexdigest(),
            )
        ],
    )
    target.write_text("user changed\n", encoding="utf-8", newline="")
    token = str(manager.approve(str(preview["change_set_id"]))["approval_token"])

    with pytest.raises(PatchError, match="PRECONDITION_HASH_MISMATCH"):
        manager.apply(str(preview["change_set_id"]), token)
    assert target.read_text(encoding="utf-8") == "user changed\n"


def test_proposal_tool_only_returns_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    from devpilot import nanobot_tools

    response = {"change_set_id": "cs-1", "status": "planned", "diff": "safe diff"}

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps(response).encode("utf-8")

    def fake_urlopen(request: object, timeout: int) -> FakeResponse:
        assert "127.0.0.1:18800/v1/change-sets/preview" in str(getattr(request, "full_url", ""))
        assert timeout == 10
        request_body = json.loads(bytes(getattr(request, "data", b"")).decode("utf-8"))
        assert request_body["patches"][0]["expected_sha256"] == "a" * 64
        return FakeResponse()

    monkeypatch.setattr(nanobot_tools, "urlopen", fake_urlopen)
    result = asyncio.run(
        ProposeChangeSetTool().execute(
            project_path="E:/project",
            patches=[
                {
                    "relative_path": "main.py",
                    "operation": "create",
                    "content": "ok\n",
                    "expected_sha256": "A" * 64,
                }
            ],
        )
    )

    assert not isinstance(result, ToolResult) or not result.is_error
    payload = json.loads(str(result))
    assert payload["status"] == "planned"
    assert payload["requires_user_confirmation"] is True
    assert "devpilot_confirmed_action" in payload["next_step"]
    assert "devpilot_request_approval" not in payload["next_step"]


def test_proposal_tool_rejects_non_local_control_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVPILOT_CONTROL_PLANE_URL", "https://example.invalid")
    result = asyncio.run(
        ProposeChangeSetTool().execute(
            project_path="E:/project",
            patches=[{"relative_path": "main.py", "operation": "create", "content": "ok\n"}],
        )
    )

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert "ValueError" in str(result)


def test_execute_changeset_requires_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    from devpilot import nanobot_tools

    def unexpected_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("control plane must not be called without confirmation")

    monkeypatch.setattr(nanobot_tools, "_apply_changeset", unexpected_call)
    result = asyncio.run(
        ExecuteChangeSetTool().execute(
            change_set_id="cs-1",
            verification_profile="python-run",
            approval_token="apply-token",
            user_confirmation="不执行",
        )
    )

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert "confirmation required" in str(result)


def test_execute_changeset_applies_with_external_token_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devpilot import nanobot_tools

    responses = [
        {"change_set_id": "cs-1", "status": "applied"},
    ]

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps(responses.pop(0)).encode("utf-8")

    calls: list[tuple[str, str, dict[str, object]]] = []

    def fake_urlopen(request: object, timeout: int) -> FakeResponse:
        req = request
        body = json.loads(req.data.decode("utf-8"))
        calls.append((str(req.full_url), str(req.method), body))
        assert timeout == 30
        return FakeResponse()

    monkeypatch.setattr(nanobot_tools, "urlopen", fake_urlopen)
    result = asyncio.run(
        ExecuteChangeSetTool().execute(
            change_set_id="cs-1",
            verification_profile="python-run",
            approval_token="apply-token",
            user_confirmation="执行",
        )
    )

    assert not isinstance(result, ToolResult) or not result.is_error
    payload = json.loads(str(result))
    assert payload["status"] == "applied"
    assert [call[1] for call in calls] == ["POST"]
    assert calls[0][0].endswith("/cs-1/apply")


def test_verify_changeset_uses_external_token_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from devpilot import nanobot_tools

    class FakeResponse:
        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps(
                {"change_set_id": "cs-1", "status": "verified", "exit_code": 0}
            ).encode("utf-8")

    calls: list[tuple[str, str, dict[str, object]]] = []

    def fake_urlopen(request: object, timeout: int) -> FakeResponse:
        req = request
        calls.append(
            (
                str(req.full_url),
                str(req.method),
                json.loads(req.data.decode("utf-8")),
            )
        )
        assert timeout == 30
        return FakeResponse()

    monkeypatch.setattr(nanobot_tools, "urlopen", fake_urlopen)
    result = asyncio.run(
        VerifyChangeSetTool().execute(
            change_set_id="cs-1",
            verification_profile="python-run",
            approval_token="verify-token",
            user_confirmation="执行",
        )
    )

    assert not isinstance(result, ToolResult) or not result.is_error
    assert json.loads(str(result))["status"] == "verified"
    assert len(calls) == 1
    assert calls[0][0].endswith("/cs-1/verify")
    assert calls[0][2] == {"profile": "python-run"}


def test_execute_changeset_rejects_unsafe_id_before_control_plane_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devpilot import nanobot_tools

    def unexpected_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unsafe ChangeSet ID must not reach the control plane")

    monkeypatch.setattr(nanobot_tools, "_post_control_plane", unexpected_call)
    result = asyncio.run(
        ExecuteChangeSetTool().execute(
            change_set_id="../../etc/passwd",
            verification_profile="python-run",
            approval_token="apply-token",
            user_confirmation="执行",
        )
    )

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert "ValueError" in str(result)


def test_create_app_exposes_only_local_control_plane_routes(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path)
    app = create_app(manager=manager)
    routes = {route.path for route in app.routes}

    assert "/healthz" in routes
    assert "/v1/change-sets/preview" in routes
    assert "/v1/change-sets/{change_set_id}/apply" in routes
    assert "/v1/change-sets/{change_set_id}/rollback" in routes


def test_h5_factory_injects_durable_store_fc_backend_and_host_issuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStore(tmp_path / "candidate" / "devpilot.db")
    store.initialize()
    backend = FCSandboxBackend(
        FCSandboxConfig(
            api_key="test-key",
            api_url="https://api.example.invalid",
            domain="example.invalid",
            network_policy_mode="provider-default",
        )
    )
    monkeypatch.setenv("DEVPILOT_ENABLE_MODEL_CANDIDATES", "1")
    monkeypatch.setenv("DEVPILOT_CANDIDATE_GRANT_SECRET", "h5-test-secret-012345678901234567890")

    runner = _candidate_trial_runner_from_environment(store=store, execution_backend=backend)
    manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        store=store,
        execution_backend=backend,
        candidate_trial_runner=runner,
    )

    assert runner is not None
    assert manager.candidate_trial_runner is runner
