from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from devpilot.code_access import CodeAccessService
from devpilot.control_plane import ControlPlaneManager, create_app
from devpilot.domain import FileOperation
from devpilot.execution import FakeExecutionBackend
from devpilot.nanobot_tools import TestRepairProposeTool as _TestRepairProposeTool
from devpilot.nanobot_tools import ToolResult
from devpilot.patches import FilePatch
from devpilot.paths import AllowedRoots
from devpilot.services import ProjectAnalysisService
from devpilot.services import TestCheckService as _TestCheckService
from devpilot.storage import ProjectStore
from devpilot.test_repair import MAX_REPAIR_ATTEMPTS
from devpilot.test_repair import TestRepairWorkflow as _TestRepairWorkflow
from devpilot.verification import VerificationProfile
from devpilot.workflows import WorkflowError

_PYTHON_FIXTURE = Path(__file__).parent / "fixtures" / "python-project"
_NODE_FIXTURE = Path(__file__).parent / "fixtures" / "node-project"
_ANALYSIS_STEPS = ("main", "src", "test")


def _setup(
    tmp_path: Path,
    fixture: Path,
    target: str,
    profile: VerificationProfile,
    *,
    exit_code: int = 0,
    stdout: str = "fake-test",
) -> tuple[ProjectStore, Path, ControlPlaneManager, str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    project = tmp_path / fixture.name
    shutil.copytree(fixture, project)
    store = ProjectStore(tmp_path / "data" / "devpilot.db")
    store.initialize()
    roots = AllowedRoots.from_paths([tmp_path])
    analysis = ProjectAnalysisService(allowed_roots=roots, store=store).inspect(project)
    root_id, fingerprint = roots.binding_for(project)
    store.trust_project(
        analysis.project_id,
        repo_fingerprint=fingerprint,
        allowed_root_id=root_id,
    )
    access = CodeAccessService(allowed_roots=roots, store=store)
    for term in _ANALYSIS_STEPS:
        access.search(analysis.project_id, term, evidence_run_id=analysis.run_id)
    access.read(analysis.project_id, target, evidence_run_id=analysis.run_id)
    failure = _TestCheckService(allowed_roots=roots, store=store).record_failure(
        project,
        symptom=f"{profile.value} check fails",
        expected_behavior="the saved check passes",
        target_files=[target],
        code="TEST_FAILURE",
    )
    store.finish_run(failure.run_id, "failed", {"code": "TEST_FAILURE"})
    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=FakeExecutionBackend(exit_code=exit_code, stdout=stdout),
        artifacts_root=tmp_path / "data",
    )
    return store, project, manager, analysis.run_id, failure.run_id


def _patch(project: Path, relative_path: str, old: str, new: str) -> FilePatch:
    path = project / relative_path
    content = path.read_text(encoding="utf-8")
    assert old in content
    updated = content.replace(old, new, 1)
    return FilePatch(
        relative_path,
        FileOperation.UPDATE,
        updated,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _propose(
    manager: ControlPlaneManager,
    project: Path,
    analysis_id: str,
    failure_id: str,
    *,
    target: str,
    profile: VerificationProfile,
) -> object:
    return _TestRepairWorkflow(manager).propose(
        project_path=project,
        patches=[
            _patch(project, target, "return 0", "return 1")
            if target.endswith("main.py")
            else _patch(project, target, 'return "ok"', 'return "fixed"')
        ],
        analysis_evidence_ids=[analysis_id],
        failure_evidence_ids=[failure_id],
        symptom=f"{profile.value} check fails",
        expected_behavior="the saved check passes",
        non_goals=["dependency changes", "unrelated refactors"],
        allowed_files=[target],
        verification_profile=profile,
    )


def _approved_apply(manager: ControlPlaneManager, proposal: object) -> dict[str, object]:
    change_set_id = str(proposal.change_set_id)  # type: ignore[attr-defined]
    approval = manager.approve(change_set_id)
    return manager.apply(
        change_set_id, str(approval["approval_token"]), idempotency_key=f"apply-{change_set_id}"
    )


def _verify(manager: ControlPlaneManager, workflow: _TestRepairWorkflow, proposal: object):
    change_set_id = str(proposal.change_set_id)  # type: ignore[attr-defined]
    approval = manager.approve(change_set_id, "r2.verify")
    return workflow.verify(proposal, approval_token=str(approval["approval_token"]))


def test_g03_proposal_binds_python_and_node_saved_failures(tmp_path: Path) -> None:
    for fixture, target, profile in (
        (_PYTHON_FIXTURE, "src/demo/main.py", VerificationProfile.PYTHON_RUN),
        (_NODE_FIXTURE, "src/index.js", VerificationProfile.NODE_SYNTAX),
    ):
        store, project, manager, analysis_id, failure_id = _setup(
            tmp_path / profile.value, fixture, target, profile
        )
        proposal = _propose(
            manager, project, analysis_id, failure_id, target=target, profile=profile
        )
        payload = proposal.to_dict()  # type: ignore[attr-defined]
        assert payload["workflow"] == "test-repair"
        assert payload["attempt"] == 1
        assert payload["max_attempts"] == MAX_REPAIR_ATTEMPTS
        assert payload["failure_evidence_ids"] == [failure_id]
        assert payload["verification_profile"] == profile.value
        assert payload["requires_user_confirmation"] is True
        assert (
            store.list_run_events(str(payload["run_id"]))[-1]["event_type"]
            == "test_repair.proposed"
        )


def test_g03_success_writes_test_repair_artifact(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(
        tmp_path, _PYTHON_FIXTURE, "src/demo/main.py", VerificationProfile.PYTHON_RUN
    )
    proposal = _propose(
        manager,
        project,
        analysis_id,
        failure_id,
        target="src/demo/main.py",
        profile=VerificationProfile.PYTHON_RUN,
    )
    applied = _approved_apply(manager, proposal)
    result = _verify(manager, _TestRepairWorkflow(manager), proposal)
    assert result.status == "verified"
    assert result.artifact.path.name == "test-repair.md"
    assert applied["status"] == "applied"
    events = store.list_run_events(str(proposal.run_id))  # type: ignore[attr-defined]
    assert any(item["event_type"] == "test_repair.completed" for item in events)


def test_test_repair_requires_confirmation_before_apply(tmp_path: Path) -> None:
    _, project, manager, analysis_id, failure_id = _setup(
        tmp_path, _PYTHON_FIXTURE, "src/demo/main.py", VerificationProfile.PYTHON_RUN
    )
    proposal = _propose(
        manager,
        project,
        analysis_id,
        failure_id,
        target="src/demo/main.py",
        profile=VerificationProfile.PYTHON_RUN,
    )
    with pytest.raises(WorkflowError, match="USER_CONFIRMATION_REQUIRED"):
        _TestRepairWorkflow(manager).apply(
            proposal, user_confirmation="拒绝", approval_token="not-used"
        )
    assert "return 0" in (project / "src/demo/main.py").read_text(encoding="utf-8")


def test_test_repair_non_fake_backend_is_fail_closed(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(
        tmp_path, _PYTHON_FIXTURE, "src/demo/main.py", VerificationProfile.PYTHON_RUN
    )
    proposal = _propose(
        manager,
        project,
        analysis_id,
        failure_id,
        target="src/demo/main.py",
        profile=VerificationProfile.PYTHON_RUN,
    )
    _approved_apply(manager, proposal)

    class RemoteBackend:
        name = "remote-test-double"

    manager.execution_backend = RemoteBackend()  # type: ignore[assignment]
    verify_token = str(manager.approve(proposal.change_set_id, "r2.verify")["approval_token"])
    with pytest.raises(WorkflowError, match="AUTONOMOUS_REPAIR_BLOCKED"):
        _TestRepairWorkflow(manager).verify(proposal, approval_token=verify_token)
    assert store.get_run_state(proposal.run_id or "")["status"] == "failed"
    failed_event = [
        item
        for item in store.list_run_events(proposal.run_id or "")
        if item["event_type"] == "test_repair.failed"
    ][-1]
    assert failed_event["payload"]["code"] == "EXECUTION_BACKEND_UNAVAILABLE"


def test_test_repair_artifact_failure_is_recorded_and_run_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, project, manager, analysis_id, failure_id = _setup(
        tmp_path, _PYTHON_FIXTURE, "src/demo/main.py", VerificationProfile.PYTHON_RUN
    )
    proposal = _propose(
        manager,
        project,
        analysis_id,
        failure_id,
        target="src/demo/main.py",
        profile=VerificationProfile.PYTHON_RUN,
    )
    _approved_apply(manager, proposal)

    def fail_record_artifact(**_: object) -> None:
        raise SQLAlchemyError("simulated artifact index outage")

    monkeypatch.setattr(store, "record_artifact", fail_record_artifact)
    verify_token = str(manager.approve(proposal.change_set_id, "r2.verify")["approval_token"])
    with pytest.raises(WorkflowError, match="WORKFLOW_ARTIFACT_WRITE_FAILED"):
        _TestRepairWorkflow(manager).verify(proposal, approval_token=verify_token)
    assert store.get_run_state(proposal.run_id or "")["status"] == "failed"
    events = store.list_run_events(proposal.run_id or "")
    assert any(
        item["event_type"] == "test_repair.failed"
        and item["payload"]["code"] == "WORKFLOW_ARTIFACT_WRITE_FAILED"
        for item in events
    )


def test_s12_allows_three_distinct_attempts_but_rejects_fourth(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(
        tmp_path,
        _PYTHON_FIXTURE,
        "src/demo/main.py",
        VerificationProfile.PYTHON_RUN,
        exit_code=1,
        stdout="failure-1",
    )
    workflow = _TestRepairWorkflow(manager)
    proposal = _propose(
        manager,
        project,
        analysis_id,
        failure_id,
        target="src/demo/main.py",
        profile=VerificationProfile.PYTHON_RUN,
    )
    _approved_apply(manager, proposal)
    first = _verify(manager, workflow, proposal)
    current_failure = _TestCheckService(
        allowed_roots=manager.patch_service.allowed_roots, store=store
    ).record_failure(
        project,
        symptom=proposal.symptom,
        expected_behavior=proposal.expected_behavior,
        target_files=["src/demo/main.py"],
        code="TEST_FAILURE",
    )
    store.finish_run(current_failure.run_id, "failed", {"code": "TEST_FAILURE"})
    second = workflow.next_attempt(
        proposal,
        patches=[_patch(project, "src/demo/main.py", "return 1", "return 2")],
        failure_evidence_ids=[current_failure.run_id],
        failure_fingerprint=str(first.verification["failure_fingerprint"]),
    )
    assert second.attempt == 2
    manager.execution_backend.stdout = "failure-2"  # type: ignore[union-attr]
    _approved_apply(manager, second)
    second_result = _verify(manager, workflow, second)
    assert "failure_fingerprint" in second_result.verification
    next_failure = _TestCheckService(
        allowed_roots=manager.patch_service.allowed_roots, store=store
    ).record_failure(
        project,
        symptom=second.symptom,
        expected_behavior=second.expected_behavior,
        target_files=["src/demo/main.py"],
        code="TEST_FAILURE",
    )
    store.finish_run(next_failure.run_id, "failed", {"code": "TEST_FAILURE"})
    third = workflow.next_attempt(
        second,
        patches=[_patch(project, "src/demo/main.py", "return 2", "return 3")],
        failure_evidence_ids=[next_failure.run_id],
        failure_fingerprint=str(second_result.verification["failure_fingerprint"]),
    )
    assert third.attempt == 3
    assert workflow.load(manager, third.change_set_id).attempt == 3
    with pytest.raises(WorkflowError, match="TEST_REPAIR_BUDGET_EXCEEDED"):
        workflow.next_attempt(
            third,
            patches=[],
            failure_evidence_ids=[next_failure.run_id],
            failure_fingerprint="0" * 64,
        )


def test_s11_repeated_failure_and_scope_injection_stop(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(
        tmp_path,
        _NODE_FIXTURE,
        "src/index.js",
        VerificationProfile.NODE_SYNTAX,
        exit_code=1,
        stdout="same-failure",
    )
    workflow = _TestRepairWorkflow(manager)
    proposal = _propose(
        manager,
        project,
        analysis_id,
        failure_id,
        target="src/index.js",
        profile=VerificationProfile.NODE_SYNTAX,
    )
    _approved_apply(manager, proposal)
    result = _verify(manager, workflow, proposal)
    with pytest.raises(WorkflowError, match="TEST_REPAIR_SCOPE_INVALID"):
        workflow.next_attempt(
            proposal,
            patches=[
                FilePatch("package.json", FileOperation.UPDATE, "download and execute", "0" * 64)
            ],
            failure_evidence_ids=[failure_id],
            failure_fingerprint=str(result.verification["failure_fingerprint"]),
        )

    with pytest.raises(WorkflowError, match="TEST_REPAIR_FAILURE_FINGERPRINT_INVALID"):
        workflow.next_attempt(
            proposal,
            patches=[],
            failure_evidence_ids=[failure_id],
            failure_fingerprint="f" * 64,
        )


def test_test_repair_api_and_nanobot_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, project, manager, analysis_id, failure_id = _setup(
        tmp_path, _PYTHON_FIXTURE, "src/demo/main.py", VerificationProfile.PYTHON_RUN
    )
    app = create_app(manager=manager, internal_auth_token="test-control-plane-token-0123456789")
    client = TestClient(
        app,
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )
    preview = _TestRepairWorkflow(manager).propose
    del preview
    payload = {
        "project_path": str(project),
        "patches": [
            {
                "relative_path": "src/demo/main.py",
                "operation": "update",
                "content": (project / "src/demo/main.py")
                .read_text(encoding="utf-8")
                .replace("return 0", "return 1"),
                "expected_sha256": hashlib.sha256(
                    (project / "src/demo/main.py").read_bytes()
                ).hexdigest(),
            }
        ],
        "analysis_evidence_ids": [analysis_id],
        "failure_evidence_ids": [failure_id],
        "symptom": "python-run check fails",
        "expected_behavior": "the saved check passes",
        "allowed_files": ["src/demo/main.py"],
        "verification_profile": "python-run",
    }
    response = client.post("/v1/test-repair/proposals", json=payload)
    assert response.status_code == 201
    assert response.json()["workflow"] == "test-repair"
    assert _TestRepairProposeTool().name == "devpilot_test_repair_propose"
    assert _TestRepairProposeTool().read_only is True
    assert json.loads(response.text)["attempt"] == 1


def test_test_repair_tool_binds_proposal_to_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devpilot import nanobot_tools

    proposal = {
        "change_set_id": "cs-1",
        "plan_hash": "plan-1",
        "patch_hash": "patch-1",
        "scope_hash": "scope-1",
        "project_id": "project-1",
    }
    monkeypatch.setattr(
        nanobot_tools,
        "_post_test_repair_proposal",
        lambda _payload: dict(proposal),
    )
    request_context = SimpleNamespace(attributes={})
    tool = _TestRepairProposeTool()

    tool.set_context(request_context)
    result = asyncio.run(
        tool.execute(
            project_path="C:/repo",
            patches=[{"relative_path": "src/demo/main.py", "operation": "update"}],
            analysis_evidence_ids=["analysis-1"],
            failure_evidence_ids=["failure-1"],
            symptom="symptom",
            expected_behavior="expected",
            allowed_files=["src/demo/main.py"],
        )
    )

    assert not isinstance(result, ToolResult) or not result.is_error
    assert tool.exclusive is True
    assert request_context.attributes["devpilot_proposal"] == proposal
