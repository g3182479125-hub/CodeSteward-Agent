from __future__ import annotations

import asyncio
import hashlib
import io
import shutil
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

import devpilot.nanobot_tools as nanobot_tools
from devpilot.code_access import CodeAccessService
from devpilot.control_plane import ControlPlaneManager, create_app
from devpilot.domain import FileOperation
from devpilot.execution import (
    ExecutionError,
    ExecutionFile,
    ExecutionRequest,
    FakeExecutionBackend,
    validate_request,
)
from devpilot.nanobot_tools import BugFixProposeTool
from devpilot.patches import FilePatch
from devpilot.paths import AllowedRoots
from devpilot.services import ProjectAnalysisService
from devpilot.skill_manager import validate_skill
from devpilot.storage import ProjectStore
from devpilot.workflows import BugFixWorkflow, WorkflowError

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "react-form-project"


def _fixture_app() -> str:
    return (_FIXTURE_ROOT / "src" / "App.jsx").read_text(encoding="utf-8")


def _fixed_app(before: str) -> str:
    return before.replace(
        "    setSubmitted(true);", "    event.preventDefault();\n    setSubmitted(true);"
    )


def _setup(tmp_path: Path, *, exit_code: int = 0) -> tuple[
    ProjectStore, Path, ControlPlaneManager, str, str
]:
    project = tmp_path / "react-form-project"
    shutil.copytree(_FIXTURE_ROOT, project)
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
    access.search(analysis.project_id, "preventDefault", evidence_run_id=analysis.run_id)
    access.read(analysis.project_id, "src/App.jsx", evidence_run_id=analysis.run_id)
    before_hash = hashlib.sha256((project / "src" / "App.jsx").read_bytes()).hexdigest()
    failure_run = store.start_run(analysis.project_id, kind="bug-check")
    store.record_bug_check_failure(
        failure_run,
        project_id=analysis.project_id,
        symptom="submitting the form navigates away",
        expected_behavior="submitting keeps the page and shows Saved",
        target_files=["src/App.jsx"],
        before_hashes={"src/App.jsx": before_hash},
        code="FORM_DEFAULT_NAVIGATION",
    )
    store.finish_run(failure_run, "failed", {"code": "FORM_DEFAULT_NAVIGATION"})
    manager = ControlPlaneManager(
        allowed_roots=roots,
        store=store,
        execution_backend=FakeExecutionBackend(exit_code=exit_code, stdout="fake-react"),
        artifacts_root=tmp_path / "data",
    )
    return store, project, manager, analysis.run_id, failure_run


def _patch(project: Path) -> FilePatch:
    before = (project / "src" / "App.jsx").read_text(encoding="utf-8")
    return FilePatch(
        "src/App.jsx",
        FileOperation.UPDATE,
        _fixed_app(before),
        hashlib.sha256((project / "src" / "App.jsx").read_bytes()).hexdigest(),
    )


def _propose(
    manager: ControlPlaneManager,
    project: Path,
    analysis_id: str,
    failure_id: str,
):
    return BugFixWorkflow(manager, artifacts_root=manager.artifacts_root).propose(
        project_path=project,
        patches=[_patch(project)],
        analysis_evidence_ids=[analysis_id],
        failure_evidence_ids=[failure_id],
        symptom="submitting the form navigates away",
        expected_behavior="submitting keeps the page and shows Saved",
        non_goals=["dependency changes", "UI redesign"],
        allowed_files=["src/App.jsx"],
    )


def test_g02_bug_fix_success_binds_failure_and_writes_artifact(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path)
    assert "event.preventDefault()" not in _fixture_app()
    proposal = _propose(manager, project, analysis_id, failure_id)

    assert proposal.to_dict()["workflow"] == "bug-fix"
    assert proposal.preview["failure_evidence_ids"] == (failure_id,)
    assert proposal.preview["allowed_files"] == ("src/App.jsx",)
    assert proposal.preview["verification_profile"] == "react-static"
    with pytest.raises(TypeError):
        proposal.preview["workflow"] = "tampered"  # type: ignore[index]

    client = TestClient(create_app(manager=manager), client=("127.0.0.1", 50000))
    apply_approval = client.post(f"/v1/change-sets/{proposal.change_set_id}/approve")
    applied = BugFixWorkflow(manager).apply(
        proposal,
        user_confirmation="执行",
        approval_token=apply_approval.json()["approval_token"],
    )
    verify_approval = client.post(
        f"/v1/change-sets/{proposal.change_set_id}/approve",
        json={"capability": "r2.verify"},
    )
    result = BugFixWorkflow(manager).verify(
        proposal,
        approval_token=verify_approval.json()["approval_token"],
        apply_result=applied,
    )

    assert result.status == "verified"
    assert result.verification["command_profile"] == "react-static"
    assert (project / "src" / "App.jsx").read_text(encoding="utf-8") == _fixed_app(
        _fixture_app()
    )
    report = result.artifact.path.read_text(encoding="utf-8")
    assert "bug-fix" in report
    assert proposal.failure_evidence_hash in report
    assert f"Artifact ID: `{store.list_artifacts(proposal.run_id or '')[0]['id']}`" in report
    assert "before=`" in report
    assert "after=`" in report
    assert store.list_artifacts(proposal.run_id or "")


def test_g02_bug_fix_failed_verification_is_not_success(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path, exit_code=1)
    proposal = _propose(manager, project, analysis_id, failure_id)
    client = TestClient(create_app(manager=manager), client=("127.0.0.1", 50000))
    applied = BugFixWorkflow(manager).apply(
        proposal,
        user_confirmation="执行",
        approval_token=client.post(f"/v1/change-sets/{proposal.change_set_id}/approve").json()[
            "approval_token"
        ],
    )
    verify_token = client.post(
        f"/v1/change-sets/{proposal.change_set_id}/approve",
        json={"capability": "r2.verify"},
    ).json()["approval_token"]
    result = BugFixWorkflow(manager).verify(
        proposal, approval_token=verify_token, apply_result=applied
    )

    assert result.status == "verification_failed"
    assert result.artifact.path.name == "bug-fix-failure.md"
    assert "Delivery result: `failed`" in result.artifact.path.read_text(encoding="utf-8")
    assert store.get_run_state(proposal.run_id or "")["status"] == "failed"
    failed_event = [
        item
        for item in store.list_run_events(proposal.run_id or "")
        if item["event_type"] == "bug_fix.failed"
    ][-1]
    assert failed_event["payload"]["artifact_id"] == store.list_artifacts(
        proposal.run_id or ""
    )[0]["id"]


def test_g02_bug_fix_rejects_out_of_scope_patch(tmp_path: Path) -> None:
    _, project, manager, analysis_id, failure_id = _setup(tmp_path)
    workflow = BugFixWorkflow(manager)
    with pytest.raises(WorkflowError, match="BUG_FIX_SCOPE_INVALID"):
        workflow.propose(
            project_path=project,
        patches=[
                FilePatch(
                    "src/Other.jsx",
                    FileOperation.UPDATE,
                    "export default function Other() {}\n",
                    hashlib.sha256((project / "src" / "App.jsx").read_bytes()).hexdigest(),
                )
            ],
            analysis_evidence_ids=[analysis_id],
            failure_evidence_ids=[failure_id],
            symptom="submitting the form navigates away",
            expected_behavior="submitting keeps the page and shows Saved",
            non_goals=[],
            allowed_files=["src/App.jsx"],
        )
    assert not list((tmp_path / "data" / "artifacts").rglob("*"))


def test_g02_bug_fix_requires_user_confirmation_before_apply(tmp_path: Path) -> None:
    _, project, manager, analysis_id, failure_id = _setup(tmp_path)
    proposal = _propose(manager, project, analysis_id, failure_id)
    with pytest.raises(WorkflowError, match="USER_CONFIRMATION_REQUIRED"):
        BugFixWorkflow(manager).apply(
            proposal,
            user_confirmation="拒绝",
            approval_token="not-used",
        )
    assert (project / "src" / "App.jsx").read_text(encoding="utf-8") == _fixture_app()


def test_g02_bug_fix_api_and_nanobot_contract(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path)
    before_hash = hashlib.sha256((project / "src" / "App.jsx").read_bytes()).hexdigest()
    client = TestClient(create_app(manager=manager), client=("127.0.0.1", 50000))
    response = client.post(
        "/v1/bug-fix/proposals",
        json={
            "project_path": str(project),
            "patches": [
                {
                    "relative_path": "src/App.jsx",
                    "operation": "update",
                    "content": _fixed_app(_fixture_app()),
                    "expected_sha256": before_hash,
                }
            ],
            "analysis_evidence_ids": [analysis_id],
            "failure_evidence_ids": [failure_id],
            "symptom": "submitting the form navigates away",
            "expected_behavior": "submitting keeps the page and shows Saved",
            "non_goals": ["dependency changes"],
            "allowed_files": ["src/App.jsx"],
        },
    )

    assert response.status_code == 201
    assert response.json()["workflow"] == "bug-fix"
    assert BugFixProposeTool().name == "devpilot_bug_fix_propose"
    assert BugFixProposeTool().read_only is True
    assert store.get_change_plan_contract(response.json()["preview"]["plan_id"])[
        "contract_hash"
    ]


def test_react_static_is_an_allowlisted_fake_only_command() -> None:
    request = ExecutionRequest(
        run_id="verify-run",
        project_id="project",
        files=(ExecutionFile("src/App.jsx", b"export default function App() {}"),),
        command="react-static /workspace/devpilot/src/App.jsx",
        command_profile="react-static",
    )
    validate_request(request)
    with pytest.raises(ExecutionError, match="EXECUTION_COMMAND_FORBIDDEN"):
        validate_request(
            ExecutionRequest(
                run_id="verify-run",
                project_id="project",
                files=request.files,
                command="react-static /workspace/devpilot/src/App.jsx && npm test",
            )
        )


def test_bug_fix_loads_persisted_contract_after_manager_restart(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path)
    proposal = _propose(manager, project, analysis_id, failure_id)
    restarted = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        store=store,
        execution_backend=FakeExecutionBackend(),
        artifacts_root=tmp_path / "data",
    )

    loaded = BugFixWorkflow.load(restarted, proposal.change_set_id)

    assert loaded.failure_evidence_ids == proposal.failure_evidence_ids
    assert loaded.allowed_files == proposal.allowed_files
    assert loaded.symptom == proposal.symptom
    assert restarted.get(proposal.change_set_id)["bug_fix_contract"]["workflow"] == "bug-fix"


def test_bug_fix_rejects_conflicting_failure_before_hashes(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path)
    project_id = store.get_run_state(failure_id)["project_id"]
    second = store.start_run(project_id, kind="bug-check")
    store.record_bug_check_failure(
        second,
        project_id=project_id,
        symptom="submitting the form navigates away",
        expected_behavior="submitting keeps the page and shows Saved",
        target_files=["src/App.jsx"],
        before_hashes={"src/App.jsx": "0" * 64},
        code="FORM_DEFAULT_NAVIGATION",
    )
    store.finish_run(second, "failed", {"code": "FORM_DEFAULT_NAVIGATION"})

    with pytest.raises(WorkflowError, match="BUG_FIX_FAILURE_EVIDENCE_INVALID"):
        BugFixWorkflow(manager).propose(
            project_path=project,
            patches=[_patch(project)],
            analysis_evidence_ids=[analysis_id],
            failure_evidence_ids=[failure_id, second],
            symptom="submitting the form navigates away",
            expected_behavior="submitting keeps the page and shows Saved",
            non_goals=[],
            allowed_files=["src/App.jsx"],
        )


def test_bug_fix_rejects_windows_casefold_hash_conflict(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path)
    project_id = store.get_run_state(failure_id)["project_id"]
    second = store.start_run(project_id, kind="bug-check")
    store.record_bug_check_failure(
        second,
        project_id=project_id,
        symptom="submitting the form navigates away",
        expected_behavior="submitting keeps the page and shows Saved",
        target_files=["src/app.jsx"],
        before_hashes={"src/app.jsx": "0" * 64},
        code="FORM_DEFAULT_NAVIGATION",
    )
    store.finish_run(second, "failed", {"code": "FORM_DEFAULT_NAVIGATION"})

    with pytest.raises(WorkflowError, match="BUG_FIX_FAILURE_EVIDENCE_INVALID"):
        BugFixWorkflow(manager).propose(
            project_path=project,
            patches=[_patch(project)],
            analysis_evidence_ids=[analysis_id],
            failure_evidence_ids=[failure_id, second],
            symptom="submitting the form navigates away",
            expected_behavior="submitting keeps the page and shows Saved",
            non_goals=[],
            allowed_files=["src/App.jsx"],
        )


def test_bug_fix_tool_maps_payload_and_redacts_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_post(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {"workflow": "bug-fix", "requires_user_confirmation": True}

    monkeypatch.setattr(nanobot_tools, "_post_bug_fix_proposal", fake_post)
    result = asyncio.run(
        BugFixProposeTool().execute(
            project_path="C:/repo",
            patches=[{"relative_path": "src/App.jsx", "operation": "update"}],
            analysis_evidence_ids=["analysis-1"],
            failure_evidence_ids=["failure-1"],
            symptom="symptom",
            expected_behavior="expected",
            non_goals=[],
            allowed_files=["src/App.jsx"],
        )
    )

    assert not isinstance(result, nanobot_tools.ToolResult) or not result.is_error
    assert captured["failure_evidence_ids"] == ["failure-1"]
    assert "requires_user_confirmation" in str(result)
    error = HTTPError(
        "http://127.0.0.1:18800",
        409,
        "Conflict",
        {},
        io.BytesIO(b'{"error":{"code":"BUG_FIX_SCOPE_INVALID","message":"secret"}}'),
    )
    assert nanobot_tools._http_error_message(error) == "HTTP 409: BUG_FIX_SCOPE_INVALID"

    def raise_error(_: dict[str, object]) -> dict[str, object]:
        raise HTTPError(
            "http://127.0.0.1:18800",
            409,
            "Conflict",
            {},
            io.BytesIO(b'{"error":{"code":"BUG_FIX_SCOPE_INVALID"}}'),
        )

    monkeypatch.setattr(nanobot_tools, "_post_bug_fix_proposal", raise_error)
    rejected = asyncio.run(
        BugFixProposeTool().execute(
            project_path="C:/repo",
            patches=[],
            analysis_evidence_ids=[],
            failure_evidence_ids=[],
            symptom="symptom",
            expected_behavior="expected",
            non_goals=[],
            allowed_files=["src/App.jsx"],
        )
    )
    assert isinstance(rejected, nanobot_tools.ToolResult)
    assert rejected.is_error is True
    assert "BUG_FIX_SCOPE_INVALID" in str(rejected)


def test_bug_fix_skill_remains_draft_until_review_approval() -> None:
    result = validate_skill(
        Path(__file__).parents[1] / "skills" / "catalog" / "bug-fix" / "SKILL.md"
    )
    assert result.name == "bug-fix"
    assert result.status == "draft"
    assert result.reviewed is False
    assert "reviewed" in result.errors[0] or result.valid is False


def test_bug_fix_backend_unavailable_writes_failure_artifact(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path)
    proposal = _propose(manager, project, analysis_id, failure_id)
    client = TestClient(create_app(manager=manager), client=("127.0.0.1", 50000))
    apply_token = client.post(f"/v1/change-sets/{proposal.change_set_id}/approve").json()[
        "approval_token"
    ]
    BugFixWorkflow(manager).apply(
        proposal,
        user_confirmation="执行",
        approval_token=apply_token,
    )
    manager.execution_backend = None
    verify_token = client.post(
        f"/v1/change-sets/{proposal.change_set_id}/approve",
        json={"capability": "r2.verify"},
    ).json()["approval_token"]

    with pytest.raises(WorkflowError, match="AUTONOMOUS_REPAIR_BLOCKED"):
        BugFixWorkflow(manager).verify(proposal, approval_token=verify_token)
    failure_path = (
        tmp_path
        / "data"
        / "artifacts"
        / proposal.project_id
        / (proposal.run_id or "")
        / "bug-fix-failure.md"
    )
    assert failure_path.exists()
    assert store.get_run_state(proposal.run_id or "")["status"] == "failed"


def test_bug_fix_artifact_db_failure_leaves_failure_artifact_and_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path)
    proposal = _propose(manager, project, analysis_id, failure_id)
    client = TestClient(create_app(manager=manager), client=("127.0.0.1", 50000))
    applied = BugFixWorkflow(manager).apply(
        proposal,
        user_confirmation="执行",
        approval_token=client.post(f"/v1/change-sets/{proposal.change_set_id}/approve").json()[
            "approval_token"
        ],
    )
    verify_token = client.post(
        f"/v1/change-sets/{proposal.change_set_id}/approve",
        json={"capability": "r2.verify"},
    ).json()["approval_token"]

    def fail_record(**_: object) -> None:
        raise SQLAlchemyError("simulated artifact index outage")

    monkeypatch.setattr(store, "record_artifact", fail_record)
    with pytest.raises(WorkflowError, match="WORKFLOW_ARTIFACT_WRITE_FAILED"):
        BugFixWorkflow(manager).verify(
            proposal, approval_token=verify_token, apply_result=applied
        )
    failure_path = (
        tmp_path
        / "data"
        / "artifacts"
        / proposal.project_id
        / (proposal.run_id or "")
        / "bug-fix-failure.md"
    )
    assert failure_path.exists()
    events = store.list_run_events(proposal.run_id or "")
    assert any(item["event_type"] == "artifact.persistence_failed" for item in events)
