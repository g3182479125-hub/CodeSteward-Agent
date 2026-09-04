from __future__ import annotations

from datetime import UTC, datetime, timedelta

from devpilot.action_broker import (
    ActionBroker,
    BrokerActionRequest,
    ConfirmationContext,
    sign_confirmation_context,
)

NOW = datetime(2026, 9, 4, 20, 0, tzinfo=UTC)
HASHES = {
    "plan_hash": "1" * 64,
    "patch_hash": "2" * 64,
    "scope_hash": "3" * 64,
}


def _request(*, action: str = "apply", sequence: int = 1) -> BrokerActionRequest:
    issued = NOW.isoformat()
    expires = (NOW + timedelta(minutes=5)).isoformat()
    unsigned = ConfirmationContext(
        session_id="session-1",
        thread_id="thread-1",
        user_message_seq=sequence,
        confirmation_id=f"confirmation-{sequence}",
        issued_at=issued,
        expires_at=expires,
        user_event_nonce=f"nonce-{sequence}",
        signature="0" * 64,
    )
    signature = sign_confirmation_context(
        unsigned,
        b"k" * 32,
        change_set_id="change-set-1",
        project_id="project-1",
        capability={"apply": "r1.apply", "verify": "r2.verify", "rollback": "r1.rollback"}[action],
        **HASHES,
    )
    context = ConfirmationContext(**{**unsigned.__dict__, "signature": signature})
    return BrokerActionRequest(
        context=context,
        action=action,
        change_set_id="change-set-1",
        project_id="project-1",
        **HASHES,
    )


def _broker(calls: list[tuple[str, ...]]) -> ActionBroker:
    def snapshot(change_set_id: str) -> dict[str, str]:
        assert change_set_id == "change-set-1"
        return {
            "change_set_id": change_set_id,
            "project_id": "project-1",
            **HASHES,
        }

    def issue(action: str, change_set_id: str) -> str:
        calls.append(("issue", action, change_set_id))
        return "opaque-token"

    def execute(
        action: str,
        change_set_id: str,
        token: str,
        idempotency_key: str,
        profile: str | None,
    ) -> dict[str, str]:
        calls.append(("execute", action, change_set_id, token, idempotency_key, profile or ""))
        return {"status": "completed", "run_id": "run-1", "result_ref": "artifact-1"}

    return ActionBroker(
        b"k" * 32,
        snapshot_reader=snapshot,
        capability_issuer=issue,
        capability_executor=execute,
        clock=lambda: NOW,
    )


def test_broker_verifies_exact_context_and_keeps_token_inside_trusted_callbacks() -> None:
    calls: list[tuple[str, ...]] = []
    result = _broker(calls).execute(_request())

    assert result.to_dict() == {
        "status": "completed",
        "change_set_id": "change-set-1",
        "action": "apply",
        "run_id": "run-1",
        "result_ref": "artifact-1",
    }
    assert calls[0] == ("issue", "apply", "change-set-1")
    assert calls[1][0:4] == ("execute", "apply", "change-set-1", "opaque-token")
    assert "opaque-token" not in str(result.to_dict())


def test_broker_rejects_tampering_scope_and_replay_before_execution() -> None:
    calls: list[tuple[str, ...]] = []
    broker = _broker(calls)
    request = _request()
    tampered = BrokerActionRequest(**{**request.__dict__, "patch_hash": "4" * 64})
    assert broker.execute(tampered).error_code == "APPROVAL_BROKER_UNAVAILABLE"
    assert broker.execute(request).status == "completed"
    assert broker.execute(request).error_code == "APPROVAL_CONFIRMATION_REPLAYED"
    assert len(calls) == 2


def test_broker_rejects_expired_confirmation_and_bad_snapshot() -> None:
    calls: list[tuple[str, ...]] = []
    broker = _broker(calls)
    expired = _request()
    expired_context = ConfirmationContext(
        **{
            **expired.context.__dict__,
            "issued_at": (NOW - timedelta(minutes=6)).isoformat(),
            "expires_at": (NOW - timedelta(minutes=1)).isoformat(),
        }
    )
    expired = BrokerActionRequest(**{**expired.__dict__, "context": expired_context})
    assert broker.execute(expired).error_code == "APPROVAL_CONFIRMATION_EXPIRED"

    mismatch_calls: list[tuple[str, ...]] = []
    mismatch = ActionBroker(
        b"k" * 32,
        snapshot_reader=lambda _change_set_id: {"project_id": "other"},
        capability_issuer=lambda *_args: "token",
        capability_executor=lambda *_args: {"status": "completed"},
        clock=lambda: NOW,
    )
    assert mismatch.execute(_request()).error_code == "APPROVAL_SCOPE_MISMATCH"
    assert mismatch_calls == []
