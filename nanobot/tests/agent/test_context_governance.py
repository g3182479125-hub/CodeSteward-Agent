from nanobot.agent.context_governance import ContextGovernanceConfig, ContextGovernor


def test_normalize_tool_result_serializes_mapping_for_provider() -> None:
    config = ContextGovernanceConfig(
        provider=object(),
        model="test-model",
        tools=object(),
        workspace=None,
        session_key=None,
        max_tool_result_chars=1000,
    )

    result = ContextGovernor.normalize_tool_result(
        config,
        "call-1",
        "temporary_tool",
        {"length": 4},
    )

    assert result == '{"length": 4}'


def _assistant_tool_call(call_id: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": "exec", "arguments": "{}"},
        }],
    }


def test_drop_orphan_tool_results_drops_missing_tool_call_id() -> None:
    messages = [
        _assistant_tool_call("call_1"),
        {"role": "tool", "name": "exec", "content": "missing id"},
        {"role": "tool", "tool_call_id": "call_1", "name": "exec", "content": "ok"},
    ]

    result = ContextGovernor.drop_orphan_tool_results(messages)

    assert [m.get("tool_call_id") for m in result if m.get("role") == "tool"] == ["call_1"]


def test_drop_orphan_tool_results_drops_duplicate_tool_result() -> None:
    messages = [
        _assistant_tool_call("call_1"),
        {"role": "tool", "tool_call_id": "call_1", "name": "exec", "content": "first"},
        {"role": "tool", "tool_call_id": "call_1", "name": "exec", "content": "duplicate"},
    ]

    result = ContextGovernor.drop_orphan_tool_results(messages)

    tool_results = [m for m in result if m.get("role") == "tool"]
    assert len(tool_results) == 1
    assert tool_results[0]["content"] == "first"
