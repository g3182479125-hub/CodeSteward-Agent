from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_p4_feature_delivery import _evidence, _manager, _patch

from devpilot.control_plane import ControlPlaneManager, create_app
from devpilot.delivery_decisions import DeliveryDecisionWorkflow
from devpilot.domain import ChangeSetStatus
from devpilot.execution import FakeExecutionBackend
from devpilot.nanobot_tools import GetDeliveryReportTool, ToolResult
from devpilot.workflows import FeatureDeliveryWorkflow


def _delivered(
    tmp_path: Path, exit_code: int = 0
) -> tuple[object, Path, ControlPlaneManager, object]:
    store, project, manager = _manager(
        tmp_path, FakeExecutionBackend(exit_code=exit_code, stdout="not in report")
    )
    evidence_id = _evidence(project, store)
    workflow = FeatureDeliveryWorkflow(manager, artifacts_root=tmp_path / "data")
    proposal = workflow.propose(
        project_path=project, patches=[_patch(project)], evidence_ids=[evidence_id]
    )
    client = TestClient(
        create_app(manager=manager, internal_auth_token="test-control-plane-token-0123456789"),
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )
    apply_approval = client.post(f"/v1/change-sets/{proposal.change_set_id}/approve")
    applied = workflow.apply(
        proposal,
        user_confirmation="执行",
        approval_token=apply_approval.json()["approval_token"],
    )
    verify_approval = client.post(
        f"/v1/change-sets/{proposal.change_set_id}/approve",
        json={"capability": "r2.verify"},
    )
    workflow.verify(
        proposal,
        approval_token=verify_approval.json()["approval_token"],
        apply_result=applied,
    )
    return store, project, manager, proposal


def test_report_is_compact_redacted_and_exposes_recovery_actions(tmp_path: Path) -> None:
    _store, _project, manager, proposal = _delivered(tmp_path)
    decision = DeliveryDecisionWorkflow(manager)

    report = decision.report(proposal.change_set_id)
    assert report["status"] == "verified"
    assert report["workflow"] == "feature-delivery"
    assert report["available_decisions"] == ["stop", "rollback"]
    assert report["artifacts"][0]["kind"] == "delivery"
    assert report["before_manifest"]
    assert report["after_manifest"]
    assert report["rollback_manifest"]
    serialized = json.dumps(report, ensure_ascii=False)
    assert "not in report" not in serialized
    assert "stdout" not in serialized
    assert "stderr" not in serialized


def test_legacy_accepted_changeset_is_read_only(tmp_path: Path) -> None:
    _store, _project, manager, proposal = _delivered(tmp_path)
    entry = manager._entries[proposal.change_set_id]
    entry.plan.change_set.status = ChangeSetStatus.ACCEPTED

    report = DeliveryDecisionWorkflow(manager).report(proposal.change_set_id)
    assert report["status"] == "accepted"
    assert report["available_decisions"] == []
    with pytest.raises(Exception, match="CHANGESET_LEGACY_READ_ONLY"):
        manager.approve(proposal.change_set_id, capability="r1.rollback")
    with pytest.raises(Exception, match="CHANGESET_LEGACY_READ_ONLY"):
        manager.rollback(proposal.change_set_id, "legacy-token")

def test_failed_delivery_can_stop_without_claiming_success(tmp_path: Path) -> None:
    _store, _project, manager, proposal = _delivered(tmp_path, exit_code=1)
    decision = DeliveryDecisionWorkflow(manager)

    report = decision.report(proposal.change_set_id)
    assert report["status"] == "verification_failed"
    assert report["available_decisions"] == ["stop", "rollback"]
    stopped = decision.stop(proposal.change_set_id, "确认停止", idempotency_key="p4-04-stop")
    assert stopped["status"] == "verification_failed"
    assert stopped["success_claimed"] is False
    assert stopped["stop_reason"] == "NONZERO_EXIT"
    assert decision.stop(
        proposal.change_set_id, "确认停止", idempotency_key="p4-04-stop"
    ) == stopped
    after = decision.report(proposal.change_set_id)
    assert after["decision"]["type"] == "stop"
    assert after["available_decisions"] == ["rollback"]

def test_stop_requires_exact_confirmation_and_never_mints_rollback(tmp_path: Path) -> None:
    _store, _project, manager, proposal = _delivered(tmp_path)
    decision = DeliveryDecisionWorkflow(manager)

    with pytest.raises(Exception, match="USER_CONFIRMATION_REQUIRED"):
        decision.stop(proposal.change_set_id, "接受", idempotency_key="p4-04-bad")
    report = decision.report(proposal.change_set_id)
    assert report["rollback"]["capability"] == "r1.rollback"
    assert report["rollback"]["requires_approval_token"] is True
    instruction = decision.rollback(proposal.change_set_id)
    assert instruction["capability"] == "r1.rollback"
    assert instruction["files_changed"] is False
    assert manager.get(proposal.change_set_id)["status"] == "verified"


def test_delivery_report_api_and_nanobot_adapter_are_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, _project, manager, proposal = _delivered(tmp_path)
    client = TestClient(
        create_app(manager=manager, internal_auth_token="test-control-plane-token-0123456789"),
        client=("127.0.0.1", 50000),
        headers={"X-DevPilot-Internal-Token": "test-control-plane-token-0123456789"},
    )

    response = client.get(f"/v1/change-sets/{proposal.change_set_id}/delivery-report")
    assert response.status_code == 200
    assert response.json()["status"] == "verified"
    missing_key = client.post(
        f"/v1/change-sets/{proposal.change_set_id}/decision",
        json={"decision": "stop", "user_confirmation": "停止"},
    )
    assert missing_key.status_code == 409
    assert missing_key.json()["error"]["code"] == "IDEMPOTENCY_REQUIRED"
    removed_accept = client.post(
        f"/v1/change-sets/{proposal.change_set_id}/decision",
        headers={"Idempotency-Key": "p4-04-removed-accept"},
        json={"decision": "accept", "user_confirmation": "接受"},
    )
    assert removed_accept.status_code == 409
    assert removed_accept.json()["error"]["code"] == "DECISION_INVALID"
    invalid_decision = client.post(
        f"/v1/change-sets/{proposal.change_set_id}/decision",
        headers={"Idempotency-Key": "p4-04-invalid-decision"},
        json={"decision": "rollback", "user_confirmation": "确认撤销"},
    )
    assert invalid_decision.status_code == 409
    assert invalid_decision.json()["error"]["code"] == "DECISION_INVALID"

    seen: list[str] = []

    def fake_get(path: str) -> dict[str, str]:
        seen.append(path)
        return {"status": "verified"}

    monkeypatch.setattr("devpilot.nanobot_tools._get_control_plane", fake_get)
    result = asyncio.run(GetDeliveryReportTool().execute(change_set_id=proposal.change_set_id))
    assert isinstance(result, str)
    assert json.loads(result)["status"] == "verified"
    assert seen == [f"/v1/change-sets/{proposal.change_set_id}/delivery-report"]
    assert GetDeliveryReportTool().read_only is True
    assert not isinstance(result, ToolResult)
