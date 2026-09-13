from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.runner_helpers import make_run_spec
from nanobot.agent.loop_driver import (
    DriverActionKind,
    LoopCheckpoint,
    PlanThenActDriver,
    RemoteStrategyDriver,
    StepInput,
    session_message_prefix_hash,
)
from nanobot.agent.loop_driver_projection import build_remote_projection, validate_remote_decision
from nanobot.agent.loop_handoff import (
    DriverRegistry,
    LoopHandoffManager,
    ManagedLoopDriver,
    SqliteLoopHandoffStore,
)
from nanobot.bus.queue import MessageBus
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


def test_h8_can_switch_back_to_default_in_same_task_without_replaying_facts(tmp_path) -> None:
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    manager.ensure(_checkpoint())
    manager.request_switch("session-1", "plan-then-act", "1")
    planned = manager.commit_boundary("session-1", _checkpoint())
    assert planned.driver_name == "plan-then-act"

    continued = LoopCheckpoint(
        "session-1", "task-1", "project-1", 4, 10, 6,
        completed_tool_results=("call-1",),
        business_references={"run_id:run-1": "run-1"},
    )
    manager.request_switch("session-1", "default-react", "1")
    restored = manager.commit_boundary("session-1", continued)

    assert restored.driver_name == "default-react"
    assert restored.task_id == "task-1"
    assert restored.checkpoint.completed_tool_results == ("call-1",)
    assert restored.checkpoint.business_references == {"run_id:run-1": "run-1"}


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


def test_h8_remote_switch_commits_the_exact_immutable_strategy_state(tmp_path) -> None:
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    manager.ensure(_checkpoint())
    strategy = RemoteStrategyDriver("strategy-one", "a" * 64, strategy_version="1.2.3")

    manager.request_switch(
        "session-1",
        strategy.name,
        strategy.version,
        driver_state=strategy.export_state(),
    )
    manager.commit_boundary("session-1", _checkpoint())
    driver, record = manager.active_driver("session-1")

    assert isinstance(driver, RemoteStrategyDriver)
    assert driver.strategy_id == "strategy-one"
    assert driver.strategy_version == "1.2.3"
    assert record.driver_state == strategy.export_state()


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


def test_h8_failed_switch_keeps_original_driver_and_latest_checkpoint(tmp_path) -> None:
    class FailingRegistry(DriverRegistry):
        fail = False

        def restore(self, name, version, state):
            if self.fail and name == "plan-then-act":
                raise ValueError("simulated initialization failure")
            return super().restore(name, version, state)

    registry = FailingRegistry()
    manager = LoopHandoffManager(
        SqliteLoopHandoffStore(tmp_path / "devpilot.db"), registry
    )
    manager.ensure(_checkpoint())
    manager.request_switch("session-1", "plan-then-act", "1")
    registry.fail = True
    latest = LoopCheckpoint("session-1", "task-1", "project-1", 4, 9, 6)

    result = manager.commit_boundary("session-1", latest)
    assert result.status == "switch_failed"
    assert result.driver_name == "default-react"
    assert result.checkpoint == latest


def test_h8_generation_prevents_stale_owner_overwrite(tmp_path) -> None:
    store = SqliteLoopHandoffStore(tmp_path / "devpilot.db")
    manager = LoopHandoffManager(store)
    first = manager.ensure(_checkpoint())
    manager.request_switch("session-1", "plan-then-act", "1")

    with pytest.raises(RuntimeError, match="ownership changed"):
        store.save(first, expected_generation=first.owner_generation)


def test_h8_rejects_checkpoint_that_regresses_authoritative_facts(tmp_path) -> None:
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    authoritative = LoopCheckpoint(
        "session-1", "task-1", "project-1", 10, 20, 2,
        uncertain_side_effect_refs=("run_id:unknown",),
    )
    manager.ensure(authoritative)
    manager.request_switch("session-1", "plan-then-act", "1")

    with pytest.raises(RuntimeError, match="checkpoint regressed"):
        manager.commit_boundary("session-1", _checkpoint())

    driver, record = manager.active_driver("session-1")
    assert driver.name == "default-react"
    assert record.checkpoint == authoritative
    assert record.status == "switch_pending"


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
        StepInput("session-1", "task-1", "project-1", 3, 8, 1)
    )

    assert managed.name == "plan-then-act"
    assert action.kind is DriverActionKind.REQUEST_MODEL
    assert action.reason == "plan_step:understand"


def test_h8_managed_driver_pauses_for_durable_unknown_or_exhausted_budget(tmp_path) -> None:
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    manager.ensure(
        LoopCheckpoint(
            "session-1", "task-1", "project-1", 2, 4, 0,
            uncertain_side_effect_refs=("run_id:unknown",),
        )
    )
    action = ManagedLoopDriver(manager, "session-1").next_action(
        StepInput("session-1", "task-1", "project-1", 3, 5, 9)
    )
    assert action.kind is DriverActionKind.PAUSE
    assert action.reason == "uncertain_side_effect_requires_recovery"


def test_h8_plan_driver_uses_durable_step_after_restart(tmp_path) -> None:
    store = SqliteLoopHandoffStore(tmp_path / "devpilot.db")
    first = LoopHandoffManager(store)
    first.ensure(LoopCheckpoint("session-1", "task-1", "project-1", 4, 9, 5))
    first.request_switch("session-1", "plan-then-act", "1")
    first.commit_boundary(
        "session-1", LoopCheckpoint("session-1", "task-1", "project-1", 4, 9, 5)
    )

    restarted = ManagedLoopDriver(LoopHandoffManager(store), "session-1")
    action = restarted.next_action(
        StepInput("session-1", "task-1", "project-1", 4, 9, 5)
    )

    assert restarted.name == "plan-then-act"
    assert action.reason == "plan_step:act"


def test_h8_remote_projection_is_redacted_and_host_validates_action() -> None:
    step = StepInput(
        "private-session", "task-with-secret-context", "E:/private/project", 2, 5, 4,
        completed_tool_results=("call-1",), pending_tool_calls=(),
    )
    projection = build_remote_projection(step)
    serialized = str(projection)

    assert "private-session" not in serialized
    assert "task-with-secret-context" not in serialized
    assert "E:/private/project" not in serialized
    assert validate_remote_decision(
        {"schema": projection["schema"], "action": "request_model", "reason": "continue"}, step
    ).kind is DriverActionKind.REQUEST_MODEL
    with pytest.raises(ValueError, match="not allowed"):
        validate_remote_decision(
            {"schema": projection["schema"], "action": "dispatch_tools"}, step
        )


def test_h8_restore_uses_durable_unknown_facts_over_older_session_checkpoint(tmp_path) -> None:
    from nanobot.agent.loop import AgentLoop

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    loop.loop_handoff_manager = manager
    session = loop.sessions.get_or_create("cli:durable")
    old = LoopCheckpoint("cli:durable", "task", "project", 1, 1, 4)
    durable = LoopCheckpoint(
        "cli:durable", "task", "project", 2, 2, 3,
        uncertain_side_effect_refs=("run_id:unknown",),
    )
    manager.ensure(durable)
    session.metadata[loop._RUNTIME_CHECKPOINT_KEY] = {"h7_loop_checkpoint": old.to_dict()}

    assert loop._restore_runtime_checkpoint(session) is True
    paused = LoopCheckpoint.from_dict(session.metadata[loop._H7_HANDOFF_PAUSE_KEY]["h7_loop_checkpoint"])
    assert paused.uncertain_side_effect_refs == ("run_id:unknown",)


def test_h8_restore_pauses_when_sqlite_checkpoint_prefix_is_not_the_saved_session(tmp_path) -> None:
    from nanobot.agent.loop import AgentLoop

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    loop.loop_handoff_manager = manager
    session = loop.sessions.get_or_create("cli:prefix")
    session.add_message("user", "different saved prefix")
    durable = LoopCheckpoint(
        "cli:prefix",
        "task",
        "project",
        2,
        1,
        3,
        session_prefix_hash=session_message_prefix_hash(
            [{"role": "user", "content": "expected prefix"}]
        ),
    )
    manager.ensure(durable)
    session.metadata[loop._RUNTIME_CHECKPOINT_KEY] = {"h7_loop_checkpoint": durable.to_dict()}

    assert loop._restore_runtime_checkpoint(session) is True
    assert session.metadata[loop._H7_HANDOFF_PAUSE_KEY]["reason"] == "session_prefix_hash_mismatch"


@pytest.mark.asyncio
async def test_h8_loop_delegates_only_the_active_remote_strategy(tmp_path) -> None:
    from nanobot.agent.loop import AgentLoop

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    manager.ensure(_checkpoint())
    strategy = RemoteStrategyDriver("strategy-one", "c" * 64, strategy_version="1.2.3")
    manager.request_switch(
        "session-1", strategy.name, strategy.version, driver_state=strategy.export_state()
    )
    manager.commit_boundary("session-1", _checkpoint())
    controller = MagicMock()
    controller.execute_remote_strategy = AsyncMock(
        return_value={
            "schema": "h8-remote-driver-projection-v1",
            "action": "request_model",
            "reason": "continue",
        }
    )
    loop.loop_handoff_manager = manager
    loop.model_candidate_controller = controller

    decision = await loop._remote_loop_strategy_decision(
        "session-1", {"schema": "h8-remote-driver-projection-v1"}
    )

    assert decision["action"] == "request_model"
    controller.execute_remote_strategy.assert_awaited_once_with(
        "strategy-one", "1.2.3", "c" * 64, {"schema": "h8-remote-driver-projection-v1"}
    )


@pytest.mark.asyncio
async def test_h8_switch_waits_for_tool_then_continues_same_runner(tmp_path) -> None:
    """A pending switch cannot replay a tool call, but affects the next model step."""
    from nanobot.agent.runner import AgentRunner

    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    manager.ensure(LoopCheckpoint("session-1", "task-1", "project-1", 0, 0, 3))
    managed = ManagedLoopDriver(manager, "session-1")
    provider = MagicMock(spec=LLMProvider)
    calls = 0
    second_messages: list[dict] = []
    checkpoints: list[dict] = []
    requested = False

    async def chat_with_retry(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="call-1", name="read_only", arguments={})],
            )
        second_messages[:] = kwargs["messages"]
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
    assert any(
        item.get("content", "").startswith("Host execution phase: act")
        for item in second_messages
    )


@pytest.mark.asyncio
async def test_h8_remote_strategy_uses_redacted_callback_and_host_validation() -> None:
    from nanobot.agent.runner import AgentRunner

    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="finished", tool_calls=[]))
    tools = MagicMock()
    tools.get_definitions.return_value = []
    received: list[dict[str, object]] = []

    async def remote_decision(projection: dict[str, object]) -> dict[str, object]:
        received.append(projection)
        return {"schema": projection["schema"], "action": "request_model", "reason": "approved"}

    result = await AgentRunner().run(
        make_run_spec(
            provider,
            initial_messages=[{"role": "user", "content": "private prompt"}],
            tools=tools,
            model="test-model",
            max_iterations=1,
            max_tool_result_chars=4_000,
            loop_driver=RemoteStrategyDriver("strategy-1", "a" * 64),
            remote_decision_callback=remote_decision,
            session_key="private-session",
            task_id="private-task",
            project_id="E:/private/project",
        )
    )

    assert result.final_content == "finished"
    assert len(received) == 1
    assert "private prompt" not in str(received[0])
    assert "private-session" not in str(received[0])


@pytest.mark.asyncio
async def test_h8_remote_strategy_stops_when_callback_is_missing_or_invalid() -> None:
    from nanobot.agent.runner import AgentRunner

    provider = MagicMock(spec=LLMProvider)
    tools = MagicMock()
    tools.get_definitions.return_value = []
    base = dict(
        initial_messages=[{"role": "user", "content": "do work"}],
        tools=tools,
        model="test-model",
        max_iterations=1,
        max_tool_result_chars=4_000,
        loop_driver=RemoteStrategyDriver("strategy-1", "b" * 64),
    )
    unavailable = await AgentRunner().run(make_run_spec(provider, **base))
    assert unavailable.stop_reason == "remote_driver_unavailable"

    async def invalid(_projection: dict[str, object]) -> dict[str, object]:
        return {"schema": "wrong", "action": "dispatch_tools"}

    invalid_result = await AgentRunner().run(
        make_run_spec(provider, **base, remote_decision_callback=invalid)
    )
    assert invalid_result.stop_reason == "remote_driver_invalid_decision"
