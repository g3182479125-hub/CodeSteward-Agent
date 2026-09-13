from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.loop_driver import DriverActionKind, LoopCheckpoint, PlanThenActDriver, StepInput
from nanobot.agent.loop_handoff import LoopHandoffManager, ManagedLoopDriver, SqliteLoopHandoffStore
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


def _checkpoint(*, pending: tuple[str, ...] = (), uncertain: tuple[str, ...] = ()) -> LoopCheckpoint:
    return LoopCheckpoint(
        session_id="session-1", task_id="task-1", project_id="project-1",
        step=3, message_position=8, remaining_iterations=7,
        pending_tool_calls=pending, uncertain_side_effect_refs=uncertain,
    )


def test_h8_switches_at_safe_boundary_without_replacing_session(tmp_path) -> None:
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    initial = manager.ensure(_checkpoint())

    pending = manager.request_switch("session-1", "plan-then-act", "1")
    assert pending.status == "switch_pending"
    assert pending.operation_id
    manager.commit_boundary("session-1", _checkpoint())
    driver, restored = manager.active_driver("session-1")

    assert isinstance(driver, PlanThenActDriver)
    assert restored.status == "active"
    assert restored.session_id == initial.session_id
    assert restored.task_id == initial.task_id
    assert restored.owner_generation > pending.owner_generation


def test_h8_waits_for_uncertain_side_effect_before_switch(tmp_path) -> None:
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    manager.ensure(_checkpoint())
    manager.request_switch("session-1", "plan-then-act", "1")

    waiting = manager.commit_boundary("session-1", _checkpoint(uncertain=("run_id:run-1",)))
    assert waiting.status == "switch_pending"
    assert manager.active_driver("session-1")[0].name == "default-react"

    completed = manager.commit_boundary("session-1", _checkpoint())
    assert completed.status == "active"
    assert manager.active_driver("session-1")[0].name == "plan-then-act"


def test_h8_rejects_unknown_driver_without_replacing_original(tmp_path) -> None:
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    manager.ensure(_checkpoint())

    with pytest.raises(ValueError, match="unknown loop driver artifact"):
        manager.request_switch("session-1", "unknown", "1")

    assert manager.active_driver("session-1")[0].name == "default-react"


def test_h8_restores_default_when_persisted_driver_is_incompatible(tmp_path) -> None:
    store = SqliteLoopHandoffStore(tmp_path / "devpilot.db")
    manager = LoopHandoffManager(store)
    original = manager.ensure(_checkpoint())
    broken = original.__class__(
        **{
            **{field: getattr(original, field) for field in original.__dataclass_fields__},
            "driver_name": "missing-driver",
            "driver_state": {"driver_state_schema": "missing"},
            "owner_generation": original.owner_generation + 1,
        }
    )
    store.save(broken, expected_generation=original.owner_generation)

    driver, recovered = manager.recover_driver("session-1")
    assert driver.name == "default-react"
    assert recovered.status == "recovered_default"
    assert recovered.checkpoint == original.checkpoint


def test_h8_generation_prevents_stale_owner_overwrite(tmp_path) -> None:
    store = SqliteLoopHandoffStore(tmp_path / "devpilot.db")
    manager = LoopHandoffManager(store)
    first = manager.ensure(_checkpoint())
    manager.request_switch("session-1", "plan-then-act", "1")

    with pytest.raises(RuntimeError, match="ownership changed"):
        store.save(first, expected_generation=first.owner_generation)


def test_h8_plan_driver_round_trips_only_structured_state() -> None:
    driver = PlanThenActDriver(plan=("understand", "act"))
    restored = PlanThenActDriver.restore_state(driver.export_state())
    assert restored.plan == ("understand", "act")
    with pytest.raises(ValueError, match="incompatible"):
        PlanThenActDriver.restore_state({"driver_state_schema": "wrong"})


def test_h8_managed_driver_reads_new_owner_without_agent_restart(tmp_path) -> None:
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    manager.ensure(_checkpoint())
    managed = ManagedLoopDriver(manager, "session-1")
    assert managed.name == "default-react"

    manager.request_switch("session-1", "plan-then-act", "1")
    manager.commit_boundary("session-1", _checkpoint())
    action = managed.next_action(
        StepInput("session-1", "task-1", "project-1", 0, 0, 1)
    )

    assert managed.name == "plan-then-act"
    assert action.kind is DriverActionKind.REQUEST_MODEL
    assert action.reason == "plan_step:understand"


@pytest.mark.asyncio
async def test_h8_switch_waits_for_tool_then_continues_same_runner(tmp_path) -> None:
    """A pending switch cannot replay a tool call, but affects the next model step."""
    from nanobot.agent.runner import AgentRunner

    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    manager.ensure(_checkpoint())
    managed = ManagedLoopDriver(manager, "session-1")
    provider = MagicMock(spec=LLMProvider)
    calls = 0
    checkpoints: list[dict] = []
    requested = False

    async def chat_with_retry(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="call-1", name="read_only", arguments={})],
            )
        return LLMResponse(content="finished", tool_calls=[])

    async def checkpoint(payload: dict) -> None:
        nonlocal requested
        checkpoints.append(payload)
        raw = LoopCheckpoint(
            "session-1", "task-1", "project-1", int(payload["iteration"]),
            len(checkpoints), 3 - int(payload["iteration"]),
            pending_tool_calls=tuple(
                str(item["id"]) for item in payload.get("pending_tool_calls", [])
            ),
        )
        if not requested:
            manager.request_switch("session-1", "plan-then-act", "1")
            requested = True
        manager.commit_boundary("session-1", raw)

    provider.chat_with_retry = chat_with_retry
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="read result")
    result = await AgentRunner().run(
        make_run_spec(
            provider,
            initial_messages=[{"role": "user", "content": "do work"}],
            tools=tools,
            model="test-model",
            max_iterations=3,
            max_tool_result_chars=4_000,
            checkpoint_callback=checkpoint,
            loop_driver=managed,
            session_key="session-1",
            task_id="task-1",
            project_id="project-1",
        )
    )

    assert result.final_content == "finished"
    assert calls == 2
    assert tools.execute.await_count == 1
    assert managed.name == "plan-then-act"
