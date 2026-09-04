from __future__ import annotations

from nanobot.channels.websocket.runtime import WebSocketChannel


def test_gateway_binds_complete_changeset_tuple_without_exporting_key() -> None:
    channel = object.__new__(WebSocketChannel)
    channel._webui_connections = {"trusted"}
    channel._devpilot_confirmation_sequences = {}
    channel._devpilot_session_id = "instance"
    channel._devpilot_signer = WebSocketChannel._DevPilotSigner(b"k" * 32)

    attrs = channel._devpilot_confirmation_attributes(
        "trusted", "chat", "执行", "turn-1",
        {
            "change_set_id": "cs-1",
            "plan_hash": "a" * 64,
            "patch_hash": "b" * 64,
            "scope_hash": "c" * 64,
            "project_id": "project-1",
        },
    )

    confirmation = attrs["devpilot_confirmation"]
    assert confirmation["action"] == "apply"
    assert "gateway_key" not in confirmation
    assert set(confirmation["context"]) == {
        "session_id", "thread_id", "user_message_seq", "confirmation_id",
        "issued_at", "expires_at", "user_event_nonce", "signature",
    }
    assert isinstance(confirmation["signer"], WebSocketChannel._DevPilotSigner)


def test_untrusted_or_unbound_confirmation_fails_closed() -> None:
    channel = object.__new__(WebSocketChannel)
    channel._webui_connections = set()
    channel._devpilot_confirmation_sequences = {}
    assert channel._devpilot_confirmation_attributes(
        "attacker", "chat", "执行", None, {}
    ) == {}
