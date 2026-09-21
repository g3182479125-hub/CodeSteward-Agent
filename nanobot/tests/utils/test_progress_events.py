from types import SimpleNamespace

from nanobot.utils.progress_events import build_tool_event_start_payload


def test_temporary_tool_start_includes_safe_lifecycle_activity() -> None:
    event = build_tool_event_start_payload(
        SimpleNamespace(
            id="call-1",
            name="devpilot_validate_temporary_tool",
            arguments={"source": "secret"},
        )
    )

    assert event["capability_activity"] == {
        "kind": "temporary_tool",
        "phase": "validating",
        "summary": "正在校验临时工具",
    }
    assert event["arguments"] == {}
    assert "source" not in event
    assert "artifact" not in event
