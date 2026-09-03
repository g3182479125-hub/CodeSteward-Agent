from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_p4_feature_delivery import _evidence, _manager, _patch

from devpilot.control_plane import ControlPlaneManager, create_app
from devpilot.delivery_decisions import DeliveryDecisionWorkflow
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
    client = TestClient(create_app(manager=manager), client=("127.0.0.1", 50000))
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


def test_report_is_compact_redacted_and_accept_is_idempotent(tmp_path: Path) -> None:
    store, _project, manager, proposal = _delivered(tmp_path)
    decision = DeliveryDecisionWorkflow(manager)

    report = decision.report(proposal.change_set_id)
    assert report["status"] == "verified"
    assert report["workflow"] == "feature-delivery"
    assert report["available_decisions"] == ["accept", "stop", "rollback"]
    assert report["artifacts"][0]["kind"] == "delivery"
    assert report["before_manifest"]
    assert report["after_manifest"]
    assert report["rollback_manifest"]
    serialized = json.dumps(report, ensure_ascii=False)
    assert "not in report" not in serialized
    assert "stdout" not in serialized
    assert "stderr" not in serialized

    accepted = decision.accept(
        proposal.change_set_id, "确认接受", idempotency_key="p4-04-accept"
    )
    assert accepted["status"] == "accepted"
    assert accepted["files_changed"] is False
    assert decision.accept(
        proposal.change_set_id, "确认接受", idempotency_key="p4-04-accept"
    ) == accepted
    assert manager.get(proposal.change_set_id)["status"] == "accepted"
    assert store.list_run_events(proposal.run_id)[-1]["event_type"] == "delivery.accepted"
    assert decision.report(proposal.change_set_id)["available_decisions"] == []


def test_failed_delivery_can_stop_without_claiming_success(tmp_path: Path) -> None:
    _store, _project, manager, proposal = _delivered(tmp_path, exit_code=1)
    decision = DeliveryDecisionWorkflow(manager)

    report = decision.report(proposal.change_set_id)
    assert report["status"] == "verification_failed"
    assert report["available_decisions"] == ["accept", "stop", "rollback"]
    stopped = decision.stop(proposal.change_set_id, "确认停止", idempotency_key="p4-04-stop")
    assert stopped["status"] == "verification_failed"
    assert stopped["success_claimed"] is False
    assert stopped["stop_reason"] == "NONZERO_EXIT"
    after = decision.report(proposal.change_set_id)
    assert after["decision"]["type"] == "stop"
    assert after["available_decisions"] == ["rollback"]

    with pytest.raises(Exception, match="CHANGESET_DECISION_FINAL"):
        decision.accept(proposal.change_set_id, "接受", idempotency_key="p4-04-accept-after-stop")


def test_decision_requires_exact_confirmation_and_never_mints_rollback(tmp_path: Path) -> None:
    _store, _project, manager, proposal = _delivered(tmp_path)
    decision = DeliveryDecisionWorkflow(manager)

    with pytest.raises(Exception, match="USER_CONFIRMATION_REQUIRED"):
        decision.accept(proposal.change_set_id, "执行", idempotency_key="p4-04-bad")
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
    client = TestClient(create_app(manager=manager), client=("127.0.0.1", 50000))

    response = client.get(f"/v1/change-sets/{proposal.change_set_id}/delivery-report")
    assert response.status_code == 200
    assert response.json()["status"] == "verified"
    missing_key = client.post(
        f"/v1/change-sets/{proposal.change_set_id}/decision",
        json={"decision": "accept", "user_confirmation": "接受"},
    )
    assert missing_key.status_code == 409
    assert missing_key.json()["error"]["code"] == "IDEMPOTENCY_REQUIRED"

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
