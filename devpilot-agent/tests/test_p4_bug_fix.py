from __future__ import annotations

import asyncio
import hashlib
import io
import shutil
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

import devpilot.nanobot_tools as nanobot_tools
import devpilot.workflows as workflows
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
from devpilot.patches import FilePatch, PatchError
from devpilot.paths import AllowedRoots
from devpilot.services import BugCheckService, ProjectAnalysisService
from devpilot.skill_manager import validate_skill
from devpilot.storage import BugCheckFailureFact, ProjectStore
from devpilot.workflows import BugFixWorkflow, WorkflowError

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "react-form-project"


def _fixture_app() -> str:
    return (_FIXTURE_ROOT / "src" / "App.jsx").read_text(encoding="utf-8")


def _fixed_app(before: str) -> str:
    return before.replace(
        "    setSubmitted(true);", "    event.preventDefault();\n    setSubmitted(true);"
    )


def _setup(
    tmp_path: Path,
    *,
    exit_code: int = 0,
    include_search_evidence: bool = True,
    include_read_evidence: bool = True,
) -> tuple[
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
    if include_search_evidence:
        access.search(analysis.project_id, "preventDefault", evidence_run_id=analysis.run_id)
    if include_read_evidence:
        access.read(analysis.project_id, "src/App.jsx", evidence_run_id=analysis.run_id)
    failure = BugCheckService(allowed_roots=roots, store=store).record_failure(
        project,
        symptom="submitting the form navigates away",
        expected_behavior="submitting keeps the page and shows Saved",
        target_files=["src/App.jsx"],
        code="FORM_DEFAULT_NAVIGATION",
    )
    failure_run = failure.run_id
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

    client = TestClient(
        create_app(manager=manager, internal_auth_token="test-control-plane-token-0123456789"),
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )
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


def test_bug_fix_proposal_accepts_inspect_and_read_evidence_without_search(
    tmp_path: Path,
) -> None:
    _, project, manager, analysis_id, failure_id = _setup(
        tmp_path, include_search_evidence=False
    )

    proposal = _propose(manager, project, analysis_id, failure_id)

    assert proposal.analysis_evidence_ids == (analysis_id,)
    assert proposal.preview["allowed_files"] == ("src/App.jsx",)


def test_bug_fix_proposal_reuses_the_latest_complete_same_project_analysis(
    tmp_path: Path,
) -> None:
    store, project, manager, complete_analysis_id, failure_id = _setup(tmp_path)
    incomplete = ProjectAnalysisService(
        allowed_roots=manager.patch_service.allowed_roots,
        store=store,
    ).inspect(project)

    proposal = _propose(manager, project, incomplete.run_id, failure_id)

    assert proposal.analysis_evidence_ids == (complete_analysis_id,)
    assert proposal.preview["analysis_evidence_ids"] == (complete_analysis_id,)


def test_bug_fix_proposal_rejects_incomplete_analysis_without_same_project_history(
    tmp_path: Path,
) -> None:
    _, project, manager, incomplete_analysis_id, failure_id = _setup(
        tmp_path,
        include_read_evidence=False,
    )

    with pytest.raises(WorkflowError, match="WORKFLOW_EVIDENCE_INVALID"):
        _propose(manager, project, incomplete_analysis_id, failure_id)


def test_bug_fix_proposal_does_not_replace_cross_project_or_unknown_analysis(
    tmp_path: Path,
) -> None:
    store, project, manager, _analysis_id, failure_id = _setup(tmp_path)
    other_project = tmp_path / "other-project"
    shutil.copytree(_FIXTURE_ROOT, other_project)
    other_analysis = ProjectAnalysisService(
        allowed_roots=manager.patch_service.allowed_roots,
        store=store,
    ).inspect(other_project)

    with pytest.raises(WorkflowError, match="WORKFLOW_EVIDENCE_INVALID"):
        _propose(manager, project, other_analysis.run_id, failure_id)
    with pytest.raises(WorkflowError, match="WORKFLOW_EVIDENCE_INVALID"):
        _propose(manager, project, "forged-analysis-run", failure_id)


def test_bug_fix_proposal_accepts_a_natural_language_failure_summary(
    tmp_path: Path,
) -> None:
    _, project, manager, analysis_id, failure_id = _setup(tmp_path)

    proposal = BugFixWorkflow(manager, artifacts_root=manager.artifacts_root).propose(
        project_path=project,
        patches=[_patch(project)],
        analysis_evidence_ids=[analysis_id],
        failure_evidence_ids=[failure_id],
        symptom="提交表单后页面会跳转，无法显示保存结果",
        expected_behavior="提交后应留在当前页面并显示保存成功",
        non_goals=["不调整页面样式"],
        allowed_files=["src/App.jsx"],
    )

    assert proposal.failure_evidence_ids == (failure_id,)
    assert proposal.symptom == "提交表单后页面会跳转，无法显示保存结果"


def test_g02_bug_fix_failed_verification_is_not_success(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path, exit_code=1)
    proposal = _propose(manager, project, analysis_id, failure_id)
    client = TestClient(
        create_app(manager=manager, internal_auth_token="test-control-plane-token-0123456789"),
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )
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


def test_react_static_fake_checks_the_repair_invariant() -> None:
    backend = FakeExecutionBackend()
    request = ExecutionRequest(
        run_id="verify-run",
        project_id="project",
        files=(ExecutionFile("src/App.jsx", b"function App() {}"),),
        command="react-static /workspace/devpilot/src/App.jsx",
        command_profile="react-static",
    )
    result = backend.run(request)
    assert result.exit_code == 1
    assert "preventDefault" in result.stderr

    fixed = ExecutionRequest(
        run_id="verify-run-2",
        project_id="project",
        files=(ExecutionFile("src/App.jsx", b"event.preventDefault();"),),
        command="react-static /workspace/devpilot/src/App.jsx",
        command_profile="react-static",
    )
    assert backend.run(fixed).exit_code == 0


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


def test_bug_fix_verify_state_errors_do_not_create_failure_artifact(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path)
    proposal = _propose(manager, project, analysis_id, failure_id)
    client = TestClient(
        create_app(manager=manager, internal_auth_token="test-control-plane-token-0123456789"),
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )
    BugFixWorkflow(manager).apply(
        proposal,
        user_confirmation="执行",
        approval_token=client.post(f"/v1/change-sets/{proposal.change_set_id}/approve").json()[
            "approval_token"
        ],
    )

    with pytest.raises(PatchError, match="APPROVAL_INVALID"):
        BugFixWorkflow(manager).verify(proposal, approval_token="invalid-token")
    assert not list((tmp_path / "data" / "artifacts").rglob("bug-fix-failure.md"))
    assert store.get_run_state(proposal.run_id or "")["status"] == "running"


@pytest.mark.parametrize(
    "error_code",
    [
        "APPROVAL_SCOPE_MISMATCH",
        "PROJECT_TRUST_REQUIRED",
        "PERSISTENCE_CORRUPT",
        "CHANGESET_NOT_VERIFIABLE",
        "VERIFICATION_PROFILE_UNSUPPORTED",
    ],
)
def test_bug_fix_control_plane_errors_are_not_verification_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path)
    proposal = _propose(manager, project, analysis_id, failure_id)

    def reject_scope(*_: object, **__: object) -> dict[str, object]:
        raise PatchError(error_code, "control-plane error")

    monkeypatch.setattr(manager, "verify", reject_scope)
    with pytest.raises(PatchError, match=error_code):
        BugFixWorkflow(manager).verify(proposal, approval_token="scope-token")
    assert not list((tmp_path / "data" / "artifacts").rglob("bug-fix-failure.md"))
    assert store.get_run_state(proposal.run_id or "")["status"] == "running"


def test_g02_bug_fix_api_and_nanobot_contract(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path)
    before_hash = hashlib.sha256((project / "src" / "App.jsx").read_bytes()).hexdigest()
    client = TestClient(
        create_app(manager=manager, internal_auth_token="test-control-plane-token-0123456789"),
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )
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


def test_bug_check_service_computes_hashes_and_storage_rejects_alias_collision(
    tmp_path: Path,
) -> None:
    store, project, _, _, _ = _setup(tmp_path)
    roots = AllowedRoots.from_paths([tmp_path])
    outcome = BugCheckService(allowed_roots=roots, store=store).record_failure(
        project,
        symptom="symptom",
        expected_behavior="expected",
        target_files=["src/App.jsx"],
        code="CHECK_FAILED",
    )
    event = [
        item
        for item in store.list_run_events(outcome.run_id)
        if item["event_type"] == "bug_check.failed"
    ][0]
    canonical_target = store.canonical_failure_path("src/App.jsx")
    assert event["payload"]["before_hashes"][canonical_target] == hashlib.sha256(
        (project / "src" / "App.jsx").read_bytes()
    ).hexdigest()

    with pytest.raises(ValueError, match="conflict"):
        store.record_bug_check_failure_fact(
            BugCheckFailureFact._from_server(
                run_id=outcome.run_id,
                project_id=outcome.project_id,
                symptom="symptom",
                expected_behavior="expected",
                target_files=("src/App.jsx",),
                before_hashes={"src/App.jsx": "0" * 64, "src/app.jsx": "1" * 64},
                code="CHECK_FAILED",
            )
        )
    with pytest.raises(ValueError, match="BugCheckService"):
        store.record_bug_check_failure_fact(
            BugCheckFailureFact(
                run_id=outcome.run_id,
                project_id=outcome.project_id,
                symptom="symptom",
                expected_behavior="expected",
                target_files=("src/App.jsx",),
                before_hashes={"src/App.jsx": "0" * 64},
                code="CHECK_FAILED",
            )
        )
    with pytest.raises(ValueError, match="does not match the project file"):
        store.record_bug_check_failure_fact(
            BugCheckFailureFact._from_server(
                run_id=outcome.run_id,
                project_id=outcome.project_id,
                symptom="symptom",
                expected_behavior="expected",
                target_files=("src/App.jsx",),
                before_hashes={"src/App.jsx": "0" * 64},
                code="CHECK_FAILED",
            )
        )
    with pytest.raises(ValueError, match="bug_check.failed"):
        store.append_event(
            outcome.run_id,
            "bug_check.failed",
            {
                "producer": "attacker",
                "schema_version": 99,
                "before_hashes": {"src/App.jsx": "0" * 64},
            },
        )
    store.finish_run(outcome.run_id, "failed", {"code": "CHECK_FAILED"})


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
    client = TestClient(
        create_app(manager=restarted, internal_auth_token="test-control-plane-token-0123456789"),
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )
    applied = BugFixWorkflow(restarted).apply(
        loaded,
        user_confirmation="执行",
        approval_token=client.post(
            f"/v1/change-sets/{loaded.change_set_id}/approve"
        ).json()["approval_token"],
    )
    result = BugFixWorkflow(restarted).verify(
        loaded,
        approval_token=client.post(
            f"/v1/change-sets/{loaded.change_set_id}/approve",
            json={"capability": "r2.verify"},
        ).json()["approval_token"],
        apply_result=applied,
    )
    assert result.status == "verified"
    event_types = [item["event_type"] for item in store.list_run_events(loaded.run_id or "")]
    assert "changeset.applied" in event_types
    assert "bug_fix.completed" in event_types
    assert event_types[-1] == "run.completed"
    assert store.list_artifacts(loaded.run_id or "")[0]["kind"] == "bug-fix"


def test_bug_fix_non_fake_backend_is_fail_closed(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path)
    proposal = _propose(manager, project, analysis_id, failure_id)
    client = TestClient(
        create_app(manager=manager, internal_auth_token="test-control-plane-token-0123456789"),
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )
    BugFixWorkflow(manager).apply(
        proposal,
        user_confirmation="执行",
        approval_token=client.post(f"/v1/change-sets/{proposal.change_set_id}/approve").json()[
            "approval_token"
        ],
    )

    class RemoteBackend:
        name = "remote-test-double"

    manager.execution_backend = RemoteBackend()  # type: ignore[assignment]
    verify_token = client.post(
        f"/v1/change-sets/{proposal.change_set_id}/approve",
        json={"capability": "r2.verify"},
    ).json()["approval_token"]
    with pytest.raises(WorkflowError, match="AUTONOMOUS_REPAIR_BLOCKED"):
        BugFixWorkflow(manager).verify(proposal, approval_token=verify_token)
    assert store.get_run_state(proposal.run_id or "")["status"] == "failed"
    failed_event = [
        item
        for item in store.list_run_events(proposal.run_id or "")
        if item["event_type"] == "bug_fix.failed"
    ][-1]
    assert failed_event["payload"]["code"] == "EXECUTION_BACKEND_UNAVAILABLE"
    assert failed_event["payload"]["artifact"]["indexed"] is True


def test_bug_fix_rejects_conflicting_failure_before_hashes(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path)
    (project / "src" / "App.jsx").write_text(_fixture_app() + "\n// changed\n", encoding="utf-8")
    second = BugCheckService(
        allowed_roots=AllowedRoots.from_paths([tmp_path]), store=store
    ).record_failure(
        project,
        symptom="submitting the form navigates away",
        expected_behavior="submitting keeps the page and shows Saved",
        target_files=["src/App.jsx"],
        code="FORM_DEFAULT_NAVIGATION",
    ).run_id
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
    (project / "src" / "App.jsx").write_text(_fixture_app() + "\n// changed\n", encoding="utf-8")
    second = BugCheckService(
        allowed_roots=AllowedRoots.from_paths([tmp_path]), store=store
    ).record_failure(
        project,
        symptom="submitting the form navigates away",
        expected_behavior="submitting keeps the page and shows Saved",
        target_files=["src/app.jsx"],
        code="FORM_DEFAULT_NAVIGATION",
    ).run_id
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


def test_bug_fix_tool_binds_proposal_to_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = {
        "change_set_id": "cs-1",
        "plan_hash": "plan-1",
        "patch_hash": "patch-1",
        "scope_hash": "scope-1",
        "project_id": "project-1",
    }
    monkeypatch.setattr(
        nanobot_tools,
        "_post_bug_fix_proposal",
        lambda _payload: dict(proposal),
    )
    request_context = SimpleNamespace(attributes={})
    tool = BugFixProposeTool()

    tool.set_context(request_context)
    result = asyncio.run(
        tool.execute(
            project_path="C:/repo",
            patches=[{"relative_path": "src/App.jsx", "operation": "update"}],
            analysis_evidence_ids=["analysis-1"],
            failure_evidence_ids=["failure-1"],
            symptom="symptom",
            expected_behavior="expected",
            allowed_files=["src/App.jsx"],
        )
    )

    assert not isinstance(result, nanobot_tools.ToolResult) or not result.is_error
    assert tool.exclusive is True
    assert request_context.attributes["devpilot_proposal"] == proposal


def test_bug_fix_skill_is_publishable_after_review_approval() -> None:
    result = validate_skill(
        Path(__file__).parents[1] / "skills" / "catalog" / "bug-fix" / "SKILL.md"
    )
    assert result.name == "bug-fix"
    assert result.status == "active"
    assert result.reviewed is True
    assert result.valid is True


def test_bug_fix_backend_unavailable_writes_failure_artifact(tmp_path: Path) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path)
    proposal = _propose(manager, project, analysis_id, failure_id)
    client = TestClient(
        create_app(manager=manager, internal_auth_token="test-control-plane-token-0123456789"),
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )
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
    client = TestClient(
        create_app(manager=manager, internal_auth_token="test-control-plane-token-0123456789"),
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )
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
    failed_event = [
        item for item in events if item["event_type"] == "bug_fix.failed"
    ][-1]
    failure_artifact = failed_event["payload"]["artifact"]
    assert failure_artifact["indexed"] is False
    assert failure_artifact["path"].endswith("bug-fix-failure.md")
    assert len(failure_artifact["sha256"]) == 64
    assert failed_event["payload"]["verification_profile"] == "react-static"
    assert failed_event["payload"]["backend"] == "fake"
    assert failed_event["payload"]["exit_code"] == 0
    assert failed_event["payload"]["before_manifest_hash"]
    assert failed_event["payload"]["after_manifest_hash"]


def test_bug_fix_artifact_file_failure_is_explicit_and_non_successful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, project, manager, analysis_id, failure_id = _setup(tmp_path)
    proposal = _propose(manager, project, analysis_id, failure_id)
    client = TestClient(
        create_app(manager=manager, internal_auth_token="test-control-plane-token-0123456789"),
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )
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

    def fail_write(*_: object, **__: object) -> object:
        raise OSError("simulated artifact filesystem outage")

    monkeypatch.setattr(workflows.ArtifactService, "write_markdown", fail_write)
    with pytest.raises(WorkflowError, match="WORKFLOW_ARTIFACT_WRITE_FAILED"):
        BugFixWorkflow(manager).verify(
            proposal, approval_token=verify_token, apply_result=applied
        )
    events = store.list_run_events(proposal.run_id or "")
    failed_event = [
        item for item in events if item["event_type"] == "bug_fix.failed"
    ][-1]
    failure_artifact = failed_event["payload"]["artifact"]
    assert failure_artifact["artifact_id"]
    assert failure_artifact["path"] is None
    assert failure_artifact["sha256"] is None
    assert failure_artifact["indexed"] is False
    assert store.get_run_state(proposal.run_id or "")["status"] == "failed"
