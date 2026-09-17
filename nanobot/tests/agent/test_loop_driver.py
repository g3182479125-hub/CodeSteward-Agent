from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.loop_driver import (
    DefaultReactDriver,
    DriverAction,
    DriverActionKind,
    LoopCheckpoint,
    StepInput,
    merge_business_facts,
)
from nanobot.agent.runner import AgentRunner
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


def test_default_react_driver_only_decides_safe_next_action() -> None:
    driver = DefaultReactDriver()
    base = dict(session_id="s", task_id="t", project_id="p", step=2, message_position=4)

    assert driver.next_action(StepInput(**base, remaining_iterations=2)).kind is DriverActionKind.REQUEST_MODEL
    assert driver.next_action(StepInput(**base, remaining_iterations=2, pending_tool_calls=("call-1",))).kind is DriverActionKind.DISPATCH_TOOLS
    assert driver.next_action(StepInput(**base, remaining_iterations=0)).kind is DriverActionKind.PAUSE


def test_checkpoint_round_trips_only_serializable_handoff_facts() -> None:
    checkpoint = LoopCheckpoint(
        session_id="s", task_id="t", project_id="p", step=2, message_position=4,
        remaining_iterations=3, completed_tool_results=("tool-1",), versions={"driver": "1"},
    )

    restored = LoopCheckpoint.from_dict(checkpoint.to_dict())

    assert restored == checkpoint
    assert "checkpoint_schema" in restored.to_dict()


def test_checkpoint_round_trips_old_tool_replay_fingerprints() -> None:
    checkpoint = LoopCheckpoint(
        session_id="s",
        task_id="t",
        project_id="p",
        step=2,
        message_position=4,
        remaining_iterations=3,
        blocked_tool_call_fingerprints=("a" * 64,),
    )

    assert LoopCheckpoint.from_dict(checkpoint.to_dict()) == checkpoint


def test_checkpoint_rejects_unknown_schema() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        LoopCheckpoint.from_dict({"checkpoint_schema": "unknown"})


def test_checkpoint_rejects_malformed_or_oversized_public_state() -> None:
    valid = LoopCheckpoint(
        session_id="s",
        task_id="t",
        project_id="p",
        step=1,
        message_position=2,
        remaining_iterations=3,
    ).to_dict()
    valid["pending_tool_calls"] = "not-a-list"
    with pytest.raises(ValueError, match="invalid loop checkpoint state"):
        LoopCheckpoint.from_dict(valid)

    valid = LoopCheckpoint(
        session_id="s", task_id="t", project_id="p", step=1, message_position=2,
        remaining_iterations=3,
    ).to_dict()
    valid["pending_tool_calls"] = [None]
    with pytest.raises(ValueError, match="invalid loop checkpoint state"):
        LoopCheckpoint.from_dict(valid)

    oversized = LoopCheckpoint(
        session_id="s",
        task_id="t",
        project_id="p",
        step=1,
        message_position=2,
        remaining_iterations=3,
        business_references={"payload": "x" * (64 * 1024)},
    )
    with pytest.raises(ValueError, match="64 KiB"):
        oversized.to_dict()
    oversized_input = {
        "checkpoint_schema": "h7-v1",
        "session_id": "s",
        "task_id": "t",
        "project_id": "p",
        "step": 1,
        "message_position": 2,
        "remaining_iterations": 3,
        "completed_tool_results": [],
        "pending_tool_calls": [],
        "versions": {},
        "business_references": {"payload": "x" * (64 * 1024)},
        "uncertain_side_effect_refs": [],
    }
    with pytest.raises(ValueError, match="64 KiB"):
        LoopCheckpoint.from_dict(oversized_input)


def test_checkpoint_blocks_driver_switch_for_pending_or_uncertain_side_effects() -> None:
    base = dict(
        session_id="s",
        task_id="t",
        project_id="p",
        step=2,
        message_position=4,
        remaining_iterations=3,
    )

    assert LoopCheckpoint(**base).switch_safe is True
    assert LoopCheckpoint(**base, pending_tool_calls=("tool-1",)).switch_safe is False
    assert LoopCheckpoint(**base, uncertain_side_effect_refs=("fc-run-1",)).switch_safe is False


def test_business_facts_keep_unknown_operation_across_later_checkpoints() -> None:
    references, uncertain = merge_business_facts(
        {},
        (),
        [{"content": '{"status":"unknown","run_id":"run-1","change_set_id":"cs-1"}'}],
        [],
    )
    references, uncertain = merge_business_facts(references, uncertain, [], [])

    assert references == {"run_id:run-1": "run-1", "change_set_id:cs-1": "cs-1"}
    assert uncertain == ("change_set_id:cs-1", "run_id:run-1")


def test_business_facts_settle_completed_pending_tool_without_losing_unknown_run() -> None:
    references, uncertain = merge_business_facts({}, (), [], [{"id": "tool-1"}])
    references, uncertain = merge_business_facts(
        references,
        uncertain,
        [{"tool_call_id": "tool-1", "content": '{"status":"unknown","run_id":"run-1"}'}],
        [],
    )

    assert references == {"run_id:run-1": "run-1"}
    assert uncertain == ("run_id:run-1",)


def test_business_facts_clear_unknown_run_after_terminal_status() -> None:
    references, uncertain = merge_business_facts(
        {},
        ("run_id:run-1", "tool_call:old-call"),
        [{"content": '{"status":"completed","run_id":"run-1"}'}],
        [],
    )

    assert references == {"run_id:run-1": "run-1"}
    assert uncertain == ()


@pytest.mark.asyncio
async def test_runner_calls_driver_at_model_and_tool_boundaries() -> None:
    class RecordingDriver:
        name = "recording"
        version = "1"

        def __init__(self) -> None:
            self.steps: list[StepInput] = []

        def next_action(self, step: StepInput) -> DriverAction:
            self.steps.append(step)
            if step.pending_tool_calls:
                return DriverAction(DriverActionKind.DISPATCH_TOOLS)
            return DriverAction(DriverActionKind.REQUEST_MODEL)

    provider = MagicMock(spec=LLMProvider)
    calls = 0

    async def chat_with_retry(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="tool-1", name="echo", arguments={})]
            )
        return LLMResponse(content="done")

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="ok")
    driver = RecordingDriver()

    result = await AgentRunner().run(make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": "run"}],
        tools=tools,
        model="test",
        max_iterations=2,
        max_tool_result_chars=10_000,
        session_key="session-1",
        task_id="task-1",
        project_id="project-1",
        loop_driver=driver,
    ))

    assert result.final_content == "done"
    assert driver.steps[0].task_id == "task-1"
    assert driver.steps[0].project_id == "project-1"
    assert driver.steps[1].pending_tool_calls == ("tool-1",)


@pytest.mark.asyncio
async def test_host_fail_closed_for_legacy_unknown_tool_set() -> None:
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="new-call", name="write_file", arguments={})],
            ),
            LLMResponse(content="continued without executing the unknown call"),
        ]
    )
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="must not run")

    result = await AgentRunner().run(make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": "continue"}],
        tools=tools,
        model="test",
        max_iterations=2,
        max_tool_result_chars=10_000,
        block_unidentified_tool_calls=True,
        blocked_tool_call_fingerprints=frozenset({"old-call-fingerprint"}),
    ))

    assert result.final_content == "continued without executing the unknown call"
    assert result.tool_events[0]["status"] == "blocked"
    tools.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_host_honors_driver_pause_before_tool_dispatch() -> None:
    class PauseBeforeToolsDriver:
        name = "pause-before-tools"
        version = "1"

        def next_action(self, step: StepInput) -> DriverAction:
            if step.pending_tool_calls:
                return DriverAction(DriverActionKind.PAUSE, "awaiting approved handoff")
            return DriverAction(DriverActionKind.REQUEST_MODEL)

    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(id="tool-1", name="echo", arguments={})],
    ))
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="must not run")
    checkpoints: list[dict] = []

    async def checkpoint(payload: dict) -> None:
        checkpoints.append(payload)

    result = await AgentRunner().run(make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": "run"}],
        tools=tools,
        model="test",
        max_iterations=1,
        max_tool_result_chars=10_000,
        loop_driver=PauseBeforeToolsDriver(),
        checkpoint_callback=checkpoint,
    ))

    assert result.stop_reason == "driver_paused"
    assert result.final_content == "awaiting approved handoff"
    tools.execute.assert_not_awaited()
    assert checkpoints[-1]["phase"] == "awaiting_tools"
    assert checkpoints[-1]["pending_tool_calls"][0]["id"] == "tool-1"
