from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from devpilot.code_access import CodeAccessService
from devpilot.control_plane import ControlPlaneManager, create_app
from devpilot.domain import FileOperation
from devpilot.execution import ExecutionRequest, ExecutionResult, FakeExecutionBackend
from devpilot.nanobot_tools import (
    FeatureDeliveryProposeTool,
    GetChangeSetTool,
    GetRunStateTool,
    ToolResult,
)
from devpilot.patches import FilePatch
from devpilot.paths import AllowedRoots
from devpilot.services import ProjectAnalysisService
from devpilot.skill_manager import validate_skill
from devpilot.storage import ArtifactRecord, ProjectStore
from devpilot.workflows import FeatureDeliveryWorkflow, WorkflowError


def _manager(tmp_path: Path, backend: object) -> tuple[ProjectStore, Path, ControlPlaneManager]:
    project = tmp_path / "fastapi-fixture"
    project.mkdir()
    store = ProjectStore(tmp_path / "data" / "devpilot.db")
    store.initialize()
    manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        store=store,
        execution_backend=backend,  # type: ignore[arg-type]
        artifacts_root=tmp_path / "data",
    )
    return store, project, manager


_BASELINE = (
    "from fastapi import FastAPI\n\n"
    "app = FastAPI()\n\n"
    '@app.get("/health")\n'
    "def health() -> dict[str, str]:\n"
    '    return {"status": "old"}\n'
)
_UPDATED = _BASELINE.replace(
    '    return {"status": "old"}\n',
    '    return {"status": "ok"}\n\n\n@app.get("/internal/status")\n'
    "def internal_status() -> dict[str, str]:\n"
    '    return {"status": "ready"}\n',
)


def _patch(project: Path) -> FilePatch:
    return FilePatch(
        "app.py",
        FileOperation.UPDATE,
        _UPDATED,
        hashlib.sha256(_BASELINE.encode()).hexdigest(),
    )


def _evidence(project: Path, store: ProjectStore) -> str:
    (project / "app.py").write_text(_BASELINE, encoding="utf-8", newline="")
    roots = AllowedRoots.from_paths([project.parent])
    outcome = ProjectAnalysisService(allowed_roots=roots, store=store).inspect(project)
    access = CodeAccessService(allowed_roots=roots, store=store)
    search = access.search(outcome.project_id, "FastAPI", evidence_run_id=outcome.run_id)
    read = access.read(outcome.project_id, "app.py", evidence_run_id=outcome.run_id)
    assert search.matches
    assert read.content
    return outcome.run_id


def test_feature_delivery_g01_completes_approved_fake_path_and_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, project, manager = _manager(tmp_path, FakeExecutionBackend(stdout="fake-ok"))
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    evidence_id = _evidence(project, store)

    proposal = workflow.propose(
        project_path=project,
        patches=[_patch(project)],
        evidence_ids=[evidence_id],
    )

    assert proposal.to_dict()["requires_user_confirmation"] is True
    assert proposal.preview["patch_hash"] == proposal.patch_hash
    assert proposal.preview["scope_hash"] == proposal.scope_hash
    assert proposal.preview["workflow"] == "feature-delivery"
    assert proposal.preview["evidence_ids"] == (evidence_id,)
    assert proposal.preview["evidence_hash"] == proposal.evidence_hash
    assert proposal.preview["check_ids"] == ("python-syntax",)
    assert "verification_failed" in proposal.preview["stop_conditions"]
    assert len(str(proposal.preview["contract_hash"])) == 64
    assert proposal.preview["contract_hash"] == store.get_change_plan_contract(
        str(proposal.preview["plan_id"])
    )["contract_hash"]
    with pytest.raises(TypeError):
        proposal.preview["status"] = "tampered"  # type: ignore[index]
    assert manager.get(proposal.change_set_id)["diff"] == proposal.preview["diff"]
    change_set_state = client = TestClient(
        create_app(manager=manager), client=("127.0.0.1", 50000)
    )
    assert change_set_state.get(f"/v1/change-sets/{proposal.change_set_id}").json()[
        "contract_hash"
    ] == proposal.preview["contract_hash"]
    assert change_set_state.get(f"/v1/runs/{evidence_id}").json()["status"] == "running"
    assert (project / "app.py").read_text(encoding="utf-8") == _BASELINE

    evidence_events = store.list_run_events(evidence_id)
    assert [item["event_type"] for item in evidence_events] == [
        "run.started",
        "tool.completed",
        "evidence.inspect.completed",
        "evidence.search.completed",
        "evidence.read.completed",
    ]
    assert proposal.run_id is not None
    proposal_events = store.list_run_events(proposal.run_id)
    assert [item["event_type"] for item in proposal_events] == [
        "run.started",
        "changeset.previewed",
        "feature_delivery.proposed",
    ]
    proposal_event = proposal_events[-1]["payload"]
    assert proposal_event["patch_hash"] == proposal.patch_hash
    assert proposal_event["scope_hash"] == proposal.scope_hash
    assert proposal_event["evidence_hash"] == proposal.evidence_hash
    assert proposal_event["contract_hash"] == proposal.preview["contract_hash"]

    # User approval is handed off through the external control-plane API; the
    # workflow only consumes the returned one-time capability.
    client = change_set_state
    apply_approval = client.post(f"/v1/change-sets/{proposal.change_set_id}/approve")
    assert apply_approval.status_code == 200
    applied = workflow.apply(
        proposal,
        user_confirmation="执行",
        approval_token=str(apply_approval.json()["approval_token"]),
    )
    verify_approval = client.post(
        f"/v1/change-sets/{proposal.change_set_id}/approve",
        json={"capability": "r2.verify"},
    )
    assert verify_approval.status_code == 200
    result = workflow.verify(
        proposal,
        approval_token=str(verify_approval.json()["approval_token"]),
        apply_result=applied,
    )

    assert result.status == "verified"
    assert result.apply["status"] == "applied"
    assert result.verification["backend"] == "fake"
    assert result.verification["exit_code"] == 0
    assert 'return {"status": "ok"}' in (project / "app.py").read_text(encoding="utf-8")
    assert '@app.get("/internal/status")' in (project / "app.py").read_text(encoding="utf-8")
    assert proposal.run_id is not None
    expected_artifact = (
        tmp_path
        / "data"
        / "artifacts"
        / proposal.project_id
        / proposal.run_id
        / "feature-delivery.md"
    )
    assert result.artifact.path == expected_artifact
    report = result.artifact.path.read_text(encoding="utf-8")
    assert "feature-delivery" in report
    assert proposal.patch_hash in report
    assert "fake-ok" not in report
    assert store.count(ArtifactRecord) == 1
    assert store.list_artifacts(proposal.run_id)[0]["kind"] == "delivery"
    assert store.list_artifacts(proposal.run_id)[0]["sha256"] == result.artifact.sha256
    assert proposal.run_id is not None
    assert store.get_run_status(proposal.run_id) == "completed"

    # The user explicitly chooses rollback through a third external capability.
    rollback_approval = client.post(
        f"/v1/change-sets/{proposal.change_set_id}/approve",
        json={"capability": "r1.rollback"},
    )
    reverted = client.post(
        f"/v1/change-sets/{proposal.change_set_id}/rollback",
        headers={
            "Authorization": f"DevPilotGrant {rollback_approval.json()['approval_token']}",
            "Idempotency-Key": "p4-g01-rollback",
        },
    )
    assert reverted.status_code == 200
    assert reverted.json()["status"] == "reverted"
    assert (project / "app.py").read_text(encoding="utf-8") == _BASELINE
    success_rollback_sha256 = hashlib.sha256((project / "app.py").read_bytes()).hexdigest()

    # The same Golden also records deterministic failed verification and
    # Artifact persistence failure; neither path may report successful delivery.
    failed_manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        store=store,
        execution_backend=FakeExecutionBackend(exit_code=1, stdout="failure-output"),
        artifacts_root=tmp_path / "data",
    )
    failed_workflow = FeatureDeliveryWorkflow(failed_manager, artifacts_root=tmp_path / "data")
    failed_proposal = failed_workflow.propose(
        project_path=project, patches=[_patch(project)], evidence_ids=[evidence_id]
    )
    failed_apply = client = TestClient(
        create_app(manager=failed_manager), client=("127.0.0.1", 50001)
    )
    failed_apply_approval = failed_apply.post(
        f"/v1/change-sets/{failed_proposal.change_set_id}/approve"
    )
    failed_applied = failed_workflow.apply(
        failed_proposal,
        user_confirmation="执行",
        approval_token=failed_apply_approval.json()["approval_token"],
    )
    failed_verify_approval = failed_apply.post(
        f"/v1/change-sets/{failed_proposal.change_set_id}/approve",
        json={"capability": "r2.verify"},
    )
    failed_result = failed_workflow.verify(
        failed_proposal,
        approval_token=failed_verify_approval.json()["approval_token"],
        apply_result=failed_applied,
    )
    assert failed_result.status == "verification_failed"
    assert failed_proposal.run_id is not None
    assert store.get_run_status(failed_proposal.run_id) == "failed"
    assert "run.completed" not in {
        item["event_type"] for item in store.list_run_events(failed_proposal.run_id)
    }
    failed_rollback = failed_manager.approve(failed_proposal.change_set_id, "r1.rollback")
    failed_manager.rollback(
        failed_proposal.change_set_id,
        str(failed_rollback["approval_token"]),
        idempotency_key="p4-g01-failed-rollback",
    )
    assert (project / "app.py").read_text(encoding="utf-8") == _BASELINE
    failed_rollback_sha256 = hashlib.sha256((project / "app.py").read_bytes()).hexdigest()

    db_failure_manager = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        store=store,
        execution_backend=FakeExecutionBackend(),
        artifacts_root=tmp_path / "data",
    )
    db_failure_workflow = FeatureDeliveryWorkflow(
        db_failure_manager, artifacts_root=tmp_path / "data"
    )
    db_failure_proposal = db_failure_workflow.propose(
        project_path=project, patches=[_patch(project)], evidence_ids=[evidence_id]
    )
    db_apply = db_failure_manager.approve(db_failure_proposal.change_set_id)
    db_applied = db_failure_workflow.apply(
        db_failure_proposal,
        user_confirmation="执行",
        approval_token=str(db_apply["approval_token"]),
    )
    db_verify = db_failure_manager.approve(db_failure_proposal.change_set_id, "r2.verify")

    def fail_record(**_kwargs: object) -> None:
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(store, "record_artifact", fail_record)
    with pytest.raises(WorkflowError, match="WORKFLOW_ARTIFACT_WRITE_FAILED"):
        db_failure_workflow.verify(
            db_failure_proposal,
            approval_token=str(db_verify["approval_token"]),
            apply_result=db_applied,
        )
    assert db_failure_proposal.run_id is not None
    assert store.get_run_status(db_failure_proposal.run_id) == "failed"
    db_events = store.list_run_events(db_failure_proposal.run_id)
    assert "run.failed" in {item["event_type"] for item in db_events}
    assert "run.completed" not in {item["event_type"] for item in db_events}
    failure_artifact_dir = (
        tmp_path
        / "data"
        / "artifacts"
        / db_failure_proposal.project_id
        / db_failure_proposal.run_id
    )
    assert list(failure_artifact_dir.glob("feature-delivery-failure.md"))
    assert not list(failure_artifact_dir.glob("feature-delivery.md"))
    assert db_failure_manager.get(db_failure_proposal.change_set_id)["status"] == "delivery_failed"
    with pytest.raises(ValueError, match="run already finished"):
        db_failure_manager.finalize_run(
            db_failure_proposal.change_set_id, "completed", {"status": "verified"}
        )
    evidence_output = os.environ.get("DEVPILOT_G01_EVIDENCE_PATH")
    if evidence_output:
        artifact_root = tmp_path / "data"
        runtime_evidence = {
            "golden": "G-01",
            "change_set_id": proposal.change_set_id,
            "run_id": proposal.run_id,
            "evidence_run_id": evidence_id,
            "patch_hash": proposal.patch_hash,
            "scope_hash": proposal.scope_hash,
            "evidence_hash": proposal.evidence_hash,
            "contract_hash": proposal.preview["contract_hash"],
            "baseline_sha256": hashlib.sha256(_BASELINE.encode()).hexdigest(),
            "rollback_sha256": success_rollback_sha256,
            "failed_rollback_sha256": failed_rollback_sha256,
            "approval_handoffs": ["r1.apply", "r2.verify", "r1.rollback"],
            "success_artifact": {
                "path": result.artifact.path.relative_to(artifact_root).as_posix(),
                "sha256": result.artifact.sha256,
                "status": store.get_run_status(proposal.run_id),
                "events": [item["event_type"] for item in store.list_run_events(proposal.run_id)],
            },
            "nonzero_failure_artifact": {
                "path": failed_result.artifact.path.relative_to(artifact_root).as_posix(),
                "sha256": failed_result.artifact.sha256,
                "status": store.get_run_status(failed_proposal.run_id),
                "events": [
                    item["event_type"] for item in store.list_run_events(failed_proposal.run_id)
                ],
            },
            "artifact_db_failure": {
                "fallback_path": next(failure_artifact_dir.glob("feature-delivery-failure.md"))
                .relative_to(artifact_root)
                .as_posix(),
                "status": store.get_run_status(db_failure_proposal.run_id),
                "change_set_status": db_failure_manager.get(db_failure_proposal.change_set_id)[
                    "status"
                ],
                "events": [item["event_type"] for item in db_events],
            },
        }
        output_path = Path(evidence_output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(runtime_evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def test_feature_delivery_run_link_survives_manager_restart(tmp_path: Path) -> None:
    store, project, manager = _manager(tmp_path, FakeExecutionBackend())
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    evidence_id = _evidence(project, store)
    proposal = workflow.propose(
        project_path=project,
        patches=[_patch(project)],
        evidence_ids=[evidence_id],
    )

    recovered = ControlPlaneManager(
        allowed_roots=AllowedRoots.from_paths([tmp_path]),
        store=store,
        execution_backend=FakeExecutionBackend(),
        artifacts_root=tmp_path / "data",
    )

    assert recovered.get_run_id(proposal.change_set_id) == proposal.run_id
    assert proposal.run_id is not None
    assert store.get_run_status(proposal.run_id) == "running"


def test_feature_delivery_http_handoff_and_explicit_rollback(tmp_path: Path) -> None:
    store, project, manager = _manager(tmp_path, FakeExecutionBackend(stdout="safe"))
    evidence_id = _evidence(project, store)
    client = TestClient(create_app(manager=manager), client=("127.0.0.1", 50000))
    response = client.post(
        "/v1/feature-delivery/proposals",
        json={
            "project_path": str(project),
            "patches": [
                {
                    "relative_path": "app.py",
                    "operation": "update",
                    "content": _UPDATED,
                    "expected_sha256": hashlib.sha256(_BASELINE.encode()).hexdigest(),
                }
            ],
            "evidence_ids": [evidence_id],
            "verification_profile": "python-syntax",
        },
    )
    assert response.status_code == 201
    payload = response.json()
    change_set_id = payload["change_set_id"]
    assert payload["requires_user_confirmation"] is True
    assert (project / "app.py").read_text(encoding="utf-8") == _BASELINE

    apply_approval = client.post(f"/v1/change-sets/{change_set_id}/approve")
    apply_token = apply_approval.json()["approval_token"]
    applied = client.post(
        f"/v1/change-sets/{change_set_id}/apply",
        headers={
            "Authorization": f"DevPilotGrant {apply_token}",
            "Idempotency-Key": "p4-http-apply",
        },
    )
    assert applied.status_code == 200
    assert (project / "app.py").read_text(encoding="utf-8") == _UPDATED

    verify_approval = client.post(
        f"/v1/change-sets/{change_set_id}/approve",
        json={"capability": "r2.verify"},
    )
    verified = client.post(
        f"/v1/change-sets/{change_set_id}/verify",
        headers={
            "Authorization": f"DevPilotGrant {verify_approval.json()['approval_token']}",
            "Idempotency-Key": "p4-http-verify",
        },
        json={"profile": "python-syntax"},
    )
    assert verified.status_code == 200
    assert verified.json()["status"] == "verified"
    assert manager.get(change_set_id)["diff"] == payload["preview"]["diff"]

    rollback_approval = client.post(
        f"/v1/change-sets/{change_set_id}/approve",
        json={"capability": "r1.rollback"},
    )
    reverted = client.post(
        f"/v1/change-sets/{change_set_id}/rollback",
        headers={
            "Authorization": f"DevPilotGrant {rollback_approval.json()['approval_token']}",
            "Idempotency-Key": "p4-http-rollback",
        },
    )
    assert reverted.status_code == 200
    assert reverted.json()["status"] == "reverted"
    assert (project / "app.py").read_text(encoding="utf-8") == _BASELINE


def test_feature_delivery_nanobot_adapter_is_proposal_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devpilot import nanobot_tools

    calls: list[dict[str, object]] = []

    def fake_proposal(payload: dict[str, object]) -> dict[str, object]:
        calls.append(payload)
        return {"change_set_id": "cs-1", "status": "awaiting_user_approval"}

    monkeypatch.setattr(nanobot_tools, "_post_feature_proposal", fake_proposal)
    result = asyncio.run(
        FeatureDeliveryProposeTool().execute(
            project_path="E:/project",
            patches=[{"relative_path": "app.py", "operation": "create", "content": "ok\n"}],
            evidence_ids=["run-1"],
        )
    )

    assert not isinstance(result, ToolResult) or not result.is_error
    assert json.loads(str(result))["requires_user_confirmation"] is True
    assert json.loads(str(result))["status"] == "awaiting_user_approval"
    assert calls[0]["evidence_ids"] == ["run-1"]


def test_feature_delivery_nanobot_adapter_maps_http_error_without_body_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devpilot import nanobot_tools

    secret = "token-shaped-value-should-not-escape"
    body = json.dumps(
        {"error": {"code": "WORKFLOW_EVIDENCE_INVALID", "message": secret}}
    ).encode()

    def rejected(_payload: dict[str, object]) -> dict[str, object]:
        raise HTTPError(
            "http://127.0.0.1:18800/v1/feature-delivery/proposals",
            409,
            "Conflict",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(nanobot_tools, "_post_feature_proposal", rejected)
    result = asyncio.run(
        FeatureDeliveryProposeTool().execute(
            project_path="E:/project",
            patches=[{"relative_path": "app.py", "operation": "create", "content": "ok\n"}],
            evidence_ids=["run-1"],
        )
    )

    assert isinstance(result, ToolResult)
    assert result.is_error
    assert "HTTP 409: WORKFLOW_EVIDENCE_INVALID" in str(result)
    assert secret not in str(result)


def test_feature_delivery_nanobot_adapter_maps_unknown_http_code_to_stable_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from devpilot import nanobot_tools

    secret = "unknown-code-secret"
    body = json.dumps({"error": {"code": secret, "message": secret}}).encode()

    def rejected(_payload: dict[str, object]) -> dict[str, object]:
        raise HTTPError(
            "http://127.0.0.1:18800/v1/feature-delivery/proposals",
            409,
            "Conflict",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(nanobot_tools, "_post_feature_proposal", rejected)
    result = asyncio.run(
        FeatureDeliveryProposeTool().execute(
            project_path="E:/project",
            patches=[{"relative_path": "app.py", "operation": "create", "content": "ok\n"}],
            evidence_ids=["run-1"],
        )
    )

    assert isinstance(result, ToolResult)
    assert "HTTP 409: CONTROL_PLANE_ERROR" in str(result)
    assert secret not in str(result)

def test_feature_delivery_records_failed_verification_and_artifact(tmp_path: Path) -> None:
    store, project, manager = _manager(tmp_path, FakeExecutionBackend(exit_code=1))
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    evidence_id = _evidence(project, store)
    proposal = workflow.propose(
        project_path=project,
        patches=[_patch(project)],
        evidence_ids=[evidence_id],
    )
    apply_approval = manager.approve(proposal.change_set_id)
    applied = workflow.apply(
        proposal,
        user_confirmation="执行",
        approval_token=str(apply_approval["approval_token"]),
    )
    verify_approval = manager.approve(proposal.change_set_id, "r2.verify")

    result = workflow.verify(
        proposal,
        approval_token=str(verify_approval["approval_token"]),
        apply_result=applied,
    )

    assert result.status == "verification_failed"
    assert result.verification["exit_code"] == 1
    assert result.artifact.path.exists()
    assert result.artifact.path.name == "feature-delivery-failure.md"
    assert "verification_failed" in result.artifact.path.read_text(encoding="utf-8")
    assert store.list_artifacts(proposal.run_id)[0]["kind"] == "delivery-failure"
    assert proposal.run_id is not None
    assert store.get_run_status(proposal.run_id) == "failed"


def test_feature_delivery_artifact_database_failure_finishes_run_as_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, project, manager = _manager(tmp_path, FakeExecutionBackend())
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    evidence_id = _evidence(project, store)
    proposal = workflow.propose(
        project_path=project,
        patches=[_patch(project)],
        evidence_ids=[evidence_id],
    )
    apply_approval = manager.approve(proposal.change_set_id)
    applied = workflow.apply(
        proposal,
        user_confirmation="执行",
        approval_token=str(apply_approval["approval_token"]),
    )
    verify_approval = manager.approve(proposal.change_set_id, "r2.verify")

    def fail_record(**_kwargs: object) -> None:
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(store, "record_artifact", fail_record)
    with pytest.raises(WorkflowError, match="WORKFLOW_ARTIFACT_WRITE_FAILED"):
        workflow.verify(
            proposal,
            approval_token=str(verify_approval["approval_token"]),
            apply_result=applied,
        )

    assert proposal.run_id is not None
    assert store.get_run_status(proposal.run_id) == "failed"


def test_feature_delivery_never_applies_without_exact_user_confirmation(tmp_path: Path) -> None:
    _store, project, manager = _manager(tmp_path, FakeExecutionBackend())
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    evidence_id = _evidence(project, _store)
    proposal = workflow.propose(
        project_path=project,
        patches=[_patch(project)],
        evidence_ids=[evidence_id],
    )

    with pytest.raises(WorkflowError, match="USER_CONFIRMATION_REQUIRED"):
        workflow.apply(
            proposal,
            user_confirmation="现在执行",
            approval_token="not-consumed",
        )

    assert (project / "app.py").read_text(encoding="utf-8") == _BASELINE


def test_feature_delivery_rejects_incomplete_or_handwritten_evidence(tmp_path: Path) -> None:
    store, project, manager = _manager(tmp_path, FakeExecutionBackend())
    FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    outcome = ProjectAnalysisService(
        allowed_roots=AllowedRoots.from_paths([tmp_path]), store=store
    ).inspect(project)

    with pytest.raises(WorkflowError, match="WORKFLOW_EVIDENCE_INVALID"):
        FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data").propose(
            project_path=project,
            patches=[_patch(project)],
            evidence_ids=[outcome.run_id],
        )


class _RogueFakeBackend(FakeExecutionBackend):
    def run(self, request: ExecutionRequest) -> ExecutionResult:
        raise AssertionError("rogue Fake subclass must not be accepted")


def test_feature_delivery_rejects_fake_subclass_before_execution(tmp_path: Path) -> None:
    store, project, manager = _manager(tmp_path, _RogueFakeBackend())
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    evidence_id = _evidence(project, store)
    proposal = workflow.propose(
        project_path=project,
        patches=[_patch(project)],
        evidence_ids=[evidence_id],
    )
    apply_approval = manager.approve(proposal.change_set_id)
    applied = workflow.apply(
        proposal, user_confirmation="执行", approval_token=str(apply_approval["approval_token"])
    )
    verify_approval = manager.approve(proposal.change_set_id, "r2.verify")

    with pytest.raises(WorkflowError, match="AUTONOMOUS_REPAIR_BLOCKED"):
        workflow.verify(
            proposal,
            approval_token=str(verify_approval["approval_token"]),
            apply_result=applied,
        )


def test_feature_delivery_read_only_query_tools_are_registered_contracts() -> None:
    assert GetChangeSetTool().name == "devpilot_get_changeset"
    assert GetRunStateTool().name == "devpilot_get_run_state"
    assert GetChangeSetTool().read_only is True
    assert GetRunStateTool().read_only is True


class _UnapprovedRemoteBackend:
    name = "fc-agent-sandbox"

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        raise AssertionError("P4 must not start real execution before the P6 Gate")


def test_feature_delivery_blocks_real_backend_before_p6(tmp_path: Path) -> None:
    _store, project, manager = _manager(tmp_path, _UnapprovedRemoteBackend())
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    evidence_id = _evidence(project, _store)
    proposal = workflow.propose(
        project_path=project,
        patches=[_patch(project)],
        evidence_ids=[evidence_id],
    )

    apply_approval = manager.approve(proposal.change_set_id)
    applied = workflow.apply(
        proposal,
        user_confirmation="执行",
        approval_token=str(apply_approval["approval_token"]),
    )
    verify_approval = manager.approve(proposal.change_set_id, "r2.verify")
    with pytest.raises(WorkflowError, match="AUTONOMOUS_REPAIR_BLOCKED"):
        workflow.verify(
            proposal,
            approval_token=str(verify_approval["approval_token"]),
            apply_result=applied,
        )

    assert (project / "app.py").read_text(encoding="utf-8") == _UPDATED


def test_feature_delivery_skill_waits_for_independent_review() -> None:
    skill = Path(__file__).parent.parent / "skills" / "catalog" / "feature-delivery" / "SKILL.md"
    result = validate_skill(skill)

    assert result.name == "feature-delivery"
    assert result.status == "draft"
    assert result.reviewed is False
    assert "metadata.devpilot.reviewed must be true before publication" in result.errors
