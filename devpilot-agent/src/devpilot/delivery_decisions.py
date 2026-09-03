"""P4-04 delivery reporting and explicit final-decision workflow."""

from __future__ import annotations

from typing import Any, cast

from .control_plane import ControlPlaneManager
from .domain import ChangeSetStatus
from .patches import PatchError

_ROLLBACK_STATES = {
    ChangeSetStatus.APPLIED.value,
    ChangeSetStatus.VERIFIED.value,
    ChangeSetStatus.VERIFICATION_FAILED.value,
    ChangeSetStatus.DELIVERY_FAILED.value,
}
_DECISION_STATES = {
    ChangeSetStatus.APPLIED.value,
    ChangeSetStatus.VERIFIED.value,
    ChangeSetStatus.VERIFICATION_FAILED.value,
    ChangeSetStatus.DELIVERY_FAILED.value,
}
_WORKFLOWS = ("feature-delivery", "bug-fix", "test-repair")
_SAFE_VERIFICATION_KEYS = (
    "backend",
    "sandbox_id",
    "profile",
    "command_profile",
    "bundle_bytes",
    "region",
    "template",
    "exit_code",
    "duration_ms",
)


class DeliveryDecisionWorkflow:
    """Expose one compact, redacted delivery view and explicit user decisions."""

    def __init__(self, manager: ControlPlaneManager) -> None:
        self.manager = manager

    def report(self, change_set_id: str) -> dict[str, Any]:
        change_set = self.manager.get(change_set_id)
        store = self.manager.store
        if store is None:
            raise PatchError("PERSISTENCE_REQUIRED", "delivery reports require durable storage")
        run_id = self.manager.get_run_id(change_set_id)
        events = store.list_run_events(run_id) if run_id is not None else []
        artifacts = store.list_artifacts(run_id) if run_id is not None else []
        persisted = store.get_change_set(change_set_id) or {}
        decision_events = [
            event
            for event in events
            if event["event_type"] in {"delivery.accepted", "delivery.stopped"}
        ]
        decision = decision_events[-1] if decision_events else None
        status = str(change_set["status"])
        workflow = self._workflow(change_set, events, store.get_run_kind(run_id) if run_id else "")
        verification_event = self._last_event(events, "changeset.verification_completed")
        failure_event = self._last_event(events, "changeset.verification_failed")
        stopped = decision is not None and decision["event_type"] == "delivery.stopped"
        available: list[str] = []
        if decision is None:
            if status in {
                ChangeSetStatus.VERIFIED.value,
                ChangeSetStatus.VERIFICATION_FAILED.value,
            }:
                available.append("accept")
            if status in _DECISION_STATES:
                available.append("stop")
        if status in _ROLLBACK_STATES and not (
            decision and decision["event_type"] == "delivery.accepted"
        ):
            available.append("rollback")
        run = store.get_run_state(run_id) if run_id is not None else None
        return {
            "change_set_id": change_set_id,
            "project_id": change_set["project_id"],
            "run_id": run_id,
            "workflow": workflow,
            "status": status,
            "run_status": run["status"] if run else None,
            "patch_hash": change_set["patch_hash"],
            "scope_hash": change_set["scope_hash"],
            "files": change_set["files"],
            "before_manifest": change_set["before_manifest"],
            "after_manifest": change_set["after_manifest"],
            "rollback_manifest": change_set["rollback_manifest"],
            "verification": self._safe_payload(
                verification_event["payload"] if verification_event else failure_event["payload"]
                if failure_event
                else None
            ),
            "failure_reason": persisted.get("failure_reason"),
            "artifacts": [self._artifact_metadata(item) for item in artifacts],
            "decision": {
                "type": (
                    "accept"
                    if decision and decision["event_type"] == "delivery.accepted"
                    else "stop"
                    if decision
                    else None
                ),
                "at": decision["created_at"] if decision else None,
                "stopped": stopped,
                "reason": (
                    decision["payload"].get("reason")
                    if decision and isinstance(decision.get("payload"), dict)
                    else None
                ),
            },
            "available_decisions": available,
            "rollback": {
                "endpoint": f"/v1/change-sets/{change_set_id}/rollback",
                "capability": "r1.rollback",
                "requires_user_confirmation": True,
                "requires_approval_token": True,
            },
        }

    def accept(
        self, change_set_id: str, user_confirmation: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self.manager.accept(
            change_set_id, user_confirmation, idempotency_key=idempotency_key
        )

    def stop(
        self, change_set_id: str, user_confirmation: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        return self.manager.stop(change_set_id, user_confirmation, idempotency_key=idempotency_key)

    def rollback(self, change_set_id: str) -> dict[str, Any]:
        """Return the existing capability-gated rollback instruction only."""
        report = self.report(change_set_id)
        if report["status"] not in _ROLLBACK_STATES:
            raise PatchError(
                "CHANGESET_NOT_ROLLBACKABLE", "ChangeSet is not at a rollback decision point"
            )
        return {
            "change_set_id": change_set_id,
            "decision": "rollback",
            "status": report["status"],
            **report["rollback"],
            "files_changed": False,
        }

    @staticmethod
    def _workflow(
        change_set: dict[str, Any], events: list[dict[str, Any]], run_kind: str
    ) -> str:
        for event in reversed(events):
            payload = event.get("payload")
            if isinstance(payload, dict):
                workflow_value = cast(dict[str, Any], payload).get("workflow")
                if workflow_value in _WORKFLOWS:
                    return str(workflow_value)
            for workflow in _WORKFLOWS:
                if event["event_type"].startswith(workflow.replace("-", "_") + "."):
                    return workflow
        return run_kind.removesuffix("-delivery") or "unknown"

    @staticmethod
    def _last_event(events: list[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
        return next(
            (event for event in reversed(events) if event["event_type"] == event_type), None
        )

    @staticmethod
    def _safe_payload(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        return {key: payload[key] for key in _SAFE_VERIFICATION_KEYS if key in payload}

    @staticmethod
    def _artifact_metadata(item: dict[str, Any]) -> dict[str, Any]:
        return {
            key: item[key]
            for key in ("id", "kind", "relative_path", "sha256", "size_bytes")
            if key in item
        }
