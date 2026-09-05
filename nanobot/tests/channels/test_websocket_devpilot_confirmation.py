from __future__ import annotations

from nanobot.channels.websocket.runtime import WebSocketChannel


def test_gateway_binds_complete_changeset_tuple_without_exporting_key() -> None:
    from devpilot.action_broker import ConfirmationContext, canonical_confirmation_string

    channel = object.__new__(WebSocketChannel)
    channel._webui_connections = {"trusted"}
    channel._devpilot_confirmation_sequences = {}
    channel._devpilot_thread_ids = {}
    channel._devpilot_changeset_threads = {}
    channel._devpilot_pending_proposals = {}
    channel._devpilot_changeset_chats = {}
    channel._devpilot_session_id = "instance"
    channel._devpilot_signer = WebSocketChannel._DevPilotSigner(b"k" * 32)
    tuple_value = {
        "change_set_id": "cs-1",
        "plan_hash": "a" * 64,
        "patch_hash": "b" * 64,
        "scope_hash": "c" * 64,
        "project_id": "project-1",
    }
    resolved = {
        **tuple_value,
        "verification_profile": "python-syntax",
        "verification_profile_version": "1",
        "verification_snapshot_hash": "d" * 64,
        "verification_check_set_hash": "e" * 64,
    }
    assert channel._register_devpilot_proposal("chat", tuple_value)

    attrs = channel._devpilot_confirmation_attributes(
        "trusted",
        "chat",
        "执行",
        "turn-1",
        resolved,
    )

    confirmation = attrs["devpilot_confirmation"]
    assert confirmation["action"] == "apply_verify"
    assert confirmation["context"]["verification_snapshot_hash"] == "d" * 64
    assert "gateway_key" not in confirmation
    assert "signer" not in confirmation
    assert set(confirmation["context"]) == {
        "session_id",
        "thread_id",
        "user_message_seq",
        "confirmation_id",
        "issued_at",
        "expires_at",
        "user_event_nonce",
        "verification_profile",
        "verification_profile_version",
        "verification_snapshot_hash",
        "verification_check_set_hash",
        "signature",
    }
    assert confirmation["context"]["thread_id"] != "chat"
    assert callable(confirmation["verifier"])
    context = ConfirmationContext.from_mapping(confirmation["context"])
    assert confirmation["verifier"](
        canonical_confirmation_string(
            context,
            change_set_id=tuple_value["change_set_id"],
            plan_hash=tuple_value["plan_hash"],
            patch_hash=tuple_value["patch_hash"],
            scope_hash=tuple_value["scope_hash"],
            project_id=tuple_value["project_id"],
            capability="r1.apply+r2.verify",
        ),
        context.signature,
    )


def test_gateway_binds_a_changeset_to_one_server_owned_thread() -> None:
    channel = object.__new__(WebSocketChannel)
    channel._webui_connections = {"trusted"}
    channel._devpilot_confirmation_sequences = {}
    channel._devpilot_session_id = "instance"
    channel._devpilot_thread_ids = {}
    channel._devpilot_changeset_threads = {}
    channel._devpilot_pending_proposals = {}
    channel._devpilot_changeset_chats = {}
    channel._devpilot_signer = WebSocketChannel._DevPilotSigner(b"k" * 32)
    tuple_value = {
        "change_set_id": "cs-1",
        "plan_hash": "a" * 64,
        "patch_hash": "b" * 64,
        "scope_hash": "c" * 64,
        "project_id": "project-1",
    }

    assert channel._register_devpilot_proposal("chat-1", tuple_value)
    resolved = {
        **tuple_value,
        "verification_profile": "python-syntax",
        "verification_profile_version": "1",
        "verification_snapshot_hash": "d" * 64,
        "verification_check_set_hash": "e" * 64,
    }
    assert channel._devpilot_confirmation_attributes(
        "trusted", "chat-1", "执行", "turn-1", resolved
    )
    assert (
        channel._devpilot_confirmation_attributes("trusted", "chat-2", "执行", "turn-2", resolved)
        == {}
    )


def test_gateway_rejects_confirmation_without_a_server_registered_proposal() -> None:
    channel = object.__new__(WebSocketChannel)
    channel._webui_connections = {"trusted"}
    channel._devpilot_confirmation_sequences = {}
    channel._devpilot_thread_ids = {}
    channel._devpilot_changeset_threads = {}
    channel._devpilot_pending_proposals = {}
    channel._devpilot_changeset_chats = {}
    channel._devpilot_session_id = "instance"
    channel._devpilot_signer = WebSocketChannel._DevPilotSigner(b"k" * 32)

    assert (
        channel._devpilot_confirmation_attributes(
            "trusted",
            "chat",
            "执行",
            "turn-1",
            {
                "change_set_id": "cs-1",
                "plan_hash": "a" * 64,
                "patch_hash": "b" * 64,
                "scope_hash": "c" * 64,
                "project_id": "project-1",
            },
        )
        == {}
    )


def test_untrusted_or_unbound_confirmation_fails_closed() -> None:
    channel = object.__new__(WebSocketChannel)
    channel._webui_connections = set()
    channel._devpilot_confirmation_sequences = {}
    assert channel._devpilot_confirmation_attributes("attacker", "chat", "执行", None, {}) == {}


def test_gateway_resolves_confirmation_against_current_control_plane(monkeypatch) -> None:
    tuple_value = {
        "change_set_id": "cs-1",
        "plan_hash": "a" * 64,
        "patch_hash": "b" * 64,
        "scope_hash": "c" * 64,
        "project_id": "project-1",
    }

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            import json

            return json.dumps(
                {
                    **tuple_value,
                    "verification_profile": "python-syntax",
                    "verification_profile_version": "1",
                    "verification_snapshot_hash": "e" * 64,
                    "verification_check_set_hash": "f" * 64,
                    "verification_available": False,
                }
            ).encode()

    monkeypatch.setattr(
        "nanobot.channels.websocket.runtime.urlopen",
        lambda _request, timeout: _Response(),
    )
    assert WebSocketChannel._resolve_devpilot_tuple(tuple_value) == {
        **tuple_value,
        "verification_profile": "python-syntax",
        "verification_profile_version": "1",
        "verification_snapshot_hash": "e" * 64,
        "verification_check_set_hash": "f" * 64,
    }

    mismatch = {**tuple_value, "patch_hash": "d" * 64}
    assert WebSocketChannel._resolve_devpilot_tuple(mismatch) is None
