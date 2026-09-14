import os
from pathlib import Path
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


def _checkpoint(
    *, pending: tuple[str, ...] = (), uncertain: tuple[str, ...] = ()
) -> LoopCheckpoint:
    return LoopCheckpoint(
        session_id="session-1",
        task_id="task-1",
        project_id="project-1",
        step=3,
        message_position=8,
        remaining_iterations=7,
        pending_tool_calls=pending,
        uncertain_side_effect_refs=uncertain,
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


def test_h9_new_task_can_start_with_selected_driver_without_replacing_old_owner(tmp_path) -> None:
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    old = manager.ensure(_checkpoint())
    selected = manager.initial_driver("plan-then-act", "1")
    new_checkpoint = LoopCheckpoint(
        "session-2",
        "task-2",
        "project-1",
        0,
        0,
        8,
    )

    new = manager.ensure(new_checkpoint, driver=selected)
    old_after_selection = manager.ensure(_checkpoint(), driver=selected)

    assert (new.driver_name, new.driver_version) == ("plan-then-act", "1")
    assert (old_after_selection.driver_name, old_after_selection.owner_generation) == (
        "default-react",
        old.owner_generation,
    )


def test_h8_can_switch_back_to_default_in_same_task_without_replaying_facts(tmp_path) -> None:
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    manager.ensure(_checkpoint())
    manager.request_switch("session-1", "plan-then-act", "1")
    planned = manager.commit_boundary("session-1", _checkpoint())
    assert planned.driver_name == "plan-then-act"

    continued = LoopCheckpoint(
        "session-1",
        "task-1",
        "project-1",
        4,
        10,
        6,
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


def test_h8_allows_the_current_host_to_settle_pending_tool_calls(tmp_path) -> None:
    """A queued switch cannot strand the already-checkpointed Host tool call."""
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    manager.ensure(_checkpoint(pending=("call-1",), uncertain=("tool_call:call-1",)))
    managed = ManagedLoopDriver(manager, "session-1")

    action = managed.next_action(
        StepInput("session-1", "task-1", "project-1", 3, 8, 7, pending_tool_calls=("call-1",))
    )
    assert action.kind is DriverActionKind.DISPATCH_TOOLS
    manager.request_switch("session-1", "plan-then-act", "1")
    assert managed.dispatch_is_current()

    manager.commit_boundary("session-1", _checkpoint())
    assert not managed.dispatch_is_current()


@pytest.mark.asyncio
async def test_h8_tool_boundary_uses_recovered_host_step() -> None:
    """A restored runner must not send its turn-local zero step to H8."""
    from nanobot.agent.runner import AgentRunner

    class RecordingDriver:
        name = "recording"
        version = "1"

        def __init__(self) -> None:
            self.steps: list[int] = []

        def next_action(self, step: StepInput):
            self.steps.append(step.step)
            return DriverAction(
                DriverActionKind.DISPATCH_TOOLS
                if step.pending_tool_calls
                else DriverActionKind.REQUEST_MODEL,
                "tools" if step.pending_tool_calls else "model",
            )

        def export_state(self):
            return {"driver_state_schema": "recording-v1"}

    from nanobot.agent.loop_driver import DriverAction

    driver = RecordingDriver()
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        side_effect=[
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="call-1", name="read_only", arguments={})],
            ),
            LLMResponse(content="finished"),
        ]
    )
    tools = MagicMock()
    tools.get_definitions.return_value = []
    tools.execute = AsyncMock(return_value="ok")

    result = await AgentRunner().run(
        make_run_spec(
            provider,
            initial_messages=[{"role": "user", "content": "work"}],
            tools=tools,
            model="test-model",
            max_iterations=2,
            max_tool_result_chars=4_000,
            loop_driver=driver,
            initial_step=4,
        )
    )

    assert result.final_content == "finished"
    assert 4 in driver.steps


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
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"), registry)
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
        "session-1",
        "task-1",
        "project-1",
        10,
        20,
        2,
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
    managed.note_checkpoint_committed(manager.commit_boundary("session-1", _checkpoint()))
    action = managed.next_action(StepInput("session-1", "task-1", "project-1", 3, 8, 1))

    assert managed.name == "plan-then-act"
    assert action.kind is DriverActionKind.REQUEST_MODEL
    assert action.reason == "plan_step:understand"


def test_h8_managed_driver_pauses_for_durable_unknown_or_exhausted_budget(tmp_path) -> None:
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    manager.ensure(
        LoopCheckpoint(
            "session-1",
            "task-1",
            "project-1",
            2,
            4,
            0,
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
    first.commit_boundary("session-1", LoopCheckpoint("session-1", "task-1", "project-1", 4, 9, 5))

    restarted = ManagedLoopDriver(LoopHandoffManager(store), "session-1")
    action = restarted.next_action(StepInput("session-1", "task-1", "project-1", 4, 9, 5))

    assert restarted.name == "plan-then-act"
    assert action.reason == "plan_step:act"


def test_h8_remote_projection_is_redacted_and_host_validates_action() -> None:
    step = StepInput(
        "private-session",
        "task-with-secret-context",
        "E:/private/project",
        2,
        5,
        4,
        completed_tool_results=("call-1",),
        pending_tool_calls=(),
    )
    projection = build_remote_projection(step)
    serialized = str(projection)

    assert "private-session" not in serialized
    assert "task-with-secret-context" not in serialized
    assert "E:/private/project" not in serialized
    assert (
        validate_remote_decision(
            {"schema": projection["schema"], "action": "request_model", "reason": "continue"}, step
        ).kind
        is DriverActionKind.REQUEST_MODEL
    )
    with pytest.raises(ValueError, match="not allowed"):
        validate_remote_decision({"schema": projection["schema"], "action": "dispatch_tools"}, step)


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
        "cli:durable",
        "task",
        "project",
        2,
        2,
        3,
        uncertain_side_effect_refs=("run_id:unknown",),
    )
    manager.ensure(durable)
    session.metadata[loop._RUNTIME_CHECKPOINT_KEY] = {"h7_loop_checkpoint": old.to_dict()}

    assert loop._restore_runtime_checkpoint(session) is True
    paused = LoopCheckpoint.from_dict(
        session.metadata[loop._H7_HANDOFF_PAUSE_KEY]["h7_loop_checkpoint"]
    )
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
        "strategy-one",
        "1.2.3",
        "c" * 64,
        {"schema": "h8-remote-driver-projection-v1"},
        project_id="project-1",
    )


@pytest.mark.asyncio
async def test_h8_agent_loop_commits_a_pending_switch_on_the_next_final_response(tmp_path) -> None:
    """The WebUI path must checkpoint the new owner, not just AgentRunner."""
    from nanobot.agent.loop import AgentLoop

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="done", tool_calls=[]))
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.get_definitions = MagicMock(return_value=[])
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    loop.loop_handoff_manager = manager
    session = loop.sessions.get_or_create("cli:h8-webui")

    session.add_message("user", "first read-only turn")
    await loop._run_agent_loop(
        [{"role": "user", "content": "first read-only turn"}],
        runtime=loop.llm_runtime(),
        session=session,
        channel="cli",
        chat_id="h8-webui",
        original_user_text="first read-only turn",
    )
    manager.request_switch(session.key, "plan-then-act", "1")

    session.add_message("user", "second read-only turn")
    await loop._run_agent_loop(
        [{"role": "user", "content": "second read-only turn"}],
        runtime=loop.llm_runtime(),
        session=session,
        channel="cli",
        chat_id="h8-webui",
        original_user_text="second read-only turn",
    )

    driver, record = manager.active_driver(session.key)
    assert isinstance(driver, PlanThenActDriver)
    assert record.status == "active"
    assert record.checkpoint.message_position == 2


@pytest.mark.asyncio
async def test_h9_agent_loop_uses_selection_only_when_creating_a_new_owner(tmp_path) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.task_profiles import DynamicPluginPolicy, TaskProfile, TaskProfileCatalog

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="done", tool_calls=[]))
    selected_scopes: list[tuple[str, Path]] = []

    def select(project_id: str, workspace: Path) -> tuple[str, str]:
        selected_scopes.append((project_id, workspace))
        return "plan-then-act", "1"

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        loop_driver_selector=select,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.task_profiles = TaskProfileCatalog(
        {
            "h9-loop": TaskProfile(
                name="h9-loop",
                skill_names=("my",),
                allowed_tools=frozenset(loop.tools.tool_names),
                output_contract=("complete",),
                dynamic_plugins=DynamicPluginPolicy(
                    enabled=True,
                    allowed_kinds=frozenset({"loop"}),
                    allowed_actions=frozenset({"activate"}),
                    allowed_scopes=frozenset({"session"}),
                ),
            )
        }
    )
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    loop.loop_handoff_manager = manager
    session = loop.sessions.get_or_create("cli:h9-selected-loop")
    session.metadata["task_profile"] = "h9-loop"

    session.add_message("user", "first turn")
    await loop._run_agent_loop(
        [{"role": "user", "content": "first turn"}],
        runtime=loop.llm_runtime(),
        session=session,
        channel="cli",
        chat_id="h9-selected-loop",
        original_user_text="first turn",
    )
    session.add_message("user", "second turn")
    await loop._run_agent_loop(
        [{"role": "user", "content": "second turn"}],
        runtime=loop.llm_runtime(),
        session=session,
        channel="cli",
        chat_id="h9-selected-loop",
        original_user_text="second turn",
    )

    driver, record = manager.active_driver(session.key)
    assert isinstance(driver, PlanThenActDriver)
    assert record.driver_name == "plan-then-act"
    assert len(selected_scopes) == 1


@pytest.mark.asyncio
async def test_h9_persisted_loop_selection_starts_only_a_new_task_owner(tmp_path) -> None:
    """H9-D: durable selection affects a new owner, never an active H8 owner."""
    import sqlite3

    from devpilot.harness_selection import HarnessSelectionStore

    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.task_profiles import DynamicPluginPolicy, TaskProfile, TaskProfileCatalog

    database = tmp_path / "devpilot.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "create table harness_selection_records (record_id text primary key, "
            "record_type text not null, payload_json text not null, record_mac text not null, "
            "created_at text not null)"
        )
    selection = HarnessSelectionStore(database, integrity_key=b"h9-loop-boundary-test-key")
    project_id = "project-h9-boundary"
    workspace = str(tmp_path.resolve())
    evidence = selection.record_evidence(
        task_id="task-evidence",
        project_id=project_id,
        workspace=workspace,
        version_hash="a" * 64,
        source_type="task_result",
        summary="host-selected loop should plan before acting",
        usage={"calls": 1},
    )
    candidate = selection.create_candidate(
        kind="loop",
        target_id="plan-then-act",
        baseline_version="0",
        candidate_version="1",
        baseline_hash="a" * 64,
        candidate_hash="b" * 64,
        scope={"project_id": project_id, "workspace": workspace},
        evidence_refs=(evidence,),
        round_id="h9-boundary",
        budget={"max_candidates": 2, "max_evaluations": 48, "max_tokens": 1000},
    )
    report = selection.run_fixed_evaluation(
        candidate.candidate_id,
        lambda category, version, _scenario, _repeat, _fixture: {
            "success": version == "candidate",
            "contract_passed": True,
            "side_effect_count": 0,
            "permission_violation": False,
            "usage": {"calls": 1, "input_tokens": 0, "output_tokens": 0},
            "category": category,
        },
    )
    confirmation = selection.issue_confirmation(
        candidate.candidate_id, user_id="user-1", session_id="selection-session"
    )

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="done", tool_calls=[]))
    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model",
        loop_driver_selector=lambda selected_project, selected_workspace: (
            selection.active_loop_driver(
                {"project_id": selected_project, "workspace": str(selected_workspace.resolve())}
            )
        ),
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.plugin_manager.project_id_for_path = MagicMock(return_value=project_id)
    loop.loop_handoff_manager = LoopHandoffManager(SqliteLoopHandoffStore(database))
    loop.task_profiles = TaskProfileCatalog(
        {"h9-loop": TaskProfile(
            name="h9-loop", skill_names=("my",), allowed_tools=frozenset(loop.tools.tool_names),
            output_contract=("complete",), dynamic_plugins=DynamicPluginPolicy(
                enabled=True, allowed_kinds=frozenset({"loop"}),
                allowed_actions=frozenset({"activate"}), allowed_scopes=frozenset({"session"}),
            ),
        )}
    )
    old_session = loop.sessions.get_or_create("cli:h9-existing-loop")
    old_session.metadata["task_profile"] = "h9-loop"
    old_session.add_message("user", "before-selection")
    await loop._run_agent_loop(
        [{"role": "user", "content": "before-selection"}],
        runtime=loop.llm_runtime(), session=old_session, channel="cli",
        chat_id="h9-existing-loop", original_user_text="before-selection",
    )
    _driver, old_record = loop.loop_handoff_manager.active_driver(old_session.key)
    assert old_record.driver_name == "default-react"

    selection.consume_confirmation(
        confirmation.confirmation_id,
        user_id="user-1",
        session_id="selection-session",
        evaluation_id=report.evaluation_id,
    )
    old_session.add_message("user", "after-selection")
    await loop._run_agent_loop(
        [{"role": "user", "content": "after-selection"}],
        runtime=loop.llm_runtime(), session=old_session, channel="cli",
        chat_id="h9-existing-loop", original_user_text="after-selection",
    )
    _driver, old_record = loop.loop_handoff_manager.active_driver(old_session.key)
    assert old_record.driver_name == "default-react"

    new_session = loop.sessions.get_or_create("cli:h9-new-loop")
    new_session.metadata["task_profile"] = "h9-loop"
    new_session.add_message("user", "after-selection")
    await loop._run_agent_loop(
        [{"role": "user", "content": "after-selection"}],
        runtime=loop.llm_runtime(), session=new_session, channel="cli",
        chat_id="h9-new-loop", original_user_text="after-selection",
    )

    driver, record = loop.loop_handoff_manager.active_driver(new_session.key)
    assert isinstance(driver, PlanThenActDriver)
    assert record.driver_name == "plan-then-act"
    assert selection.active_loop_driver({"project_id": project_id, "workspace": workspace}) == (
        "plan-then-act", "1"
    )


@pytest.mark.asyncio
async def test_h9_task_evidence_records_runner_usage_at_the_task_boundary(tmp_path) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.task_profiles import DynamicPluginPolicy, TaskProfile, TaskProfileCatalog

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(
            content="done",
            tool_calls=[],
            usage={"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
        )
    )
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.task_profiles = TaskProfileCatalog(
        {
            "h9-evidence": TaskProfile(
                name="h9-evidence",
                skill_names=("my",),
                allowed_tools=frozenset(loop.tools.tool_names),
                output_contract=("complete",),
                dynamic_plugins=DynamicPluginPolicy(
                    enabled=True,
                    allowed_kinds=frozenset({"loop"}),
                    allowed_actions=frozenset({"activate"}),
                    allowed_scopes=frozenset({"session"}),
                ),
            )
        }
    )
    evidence: list[dict[str, object]] = []
    selection_store = MagicMock()
    selection_store.record_evidence.side_effect = (
        lambda **kwargs: evidence.append(kwargs) or f"evidence-{len(evidence)}"
    )
    loop.harness_selection_store = selection_store
    session = loop.sessions.get_or_create("cli:h9-evidence")
    session.metadata.update({"task_profile": "h9-evidence", "task_profile_version": "1"})

    await loop._run_agent_loop(
        [{"role": "user", "content": "record this task"}],
        runtime=loop.llm_runtime(),
        session=session,
        channel="cli",
        chat_id="h9-evidence",
        original_user_text="record this task",
    )

    assert len(evidence) == 1
    assert evidence[0]["source_type"] == "task_result"
    assert evidence[0]["usage"] == {
        "calls": 1,
        "input_tokens": 11,
        "output_tokens": 5,
    }


@pytest.mark.asyncio
async def test_h9_agent_loop_loads_selected_plugin_only_for_a_new_owner(tmp_path) -> None:
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.task_profiles import DynamicPluginPolicy, TaskProfile, TaskProfileCatalog

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="done", tool_calls=[]))
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.loop_handoff_manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    loop.harness_selection_store = MagicMock()
    loop.harness_selection_store.active_plugin_version.return_value = ("model-greeting", "1.0.0")
    loop.plugin_manager.start_h9_selected_candidate = MagicMock()
    loop.task_profiles = TaskProfileCatalog(
        {
            "h9": TaskProfile(
                name="h9",
                skill_names=("my",),
                allowed_tools=frozenset(loop.tools.tool_names),
                output_contract=("complete",),
                dynamic_plugins=DynamicPluginPolicy(
                    enabled=True,
                    allowed_kinds=frozenset({"tool"}),
                    allowed_actions=frozenset({"activate"}),
                    allowed_scopes=frozenset({"session"}),
                ),
            )
        }
    )
    session = loop.sessions.get_or_create("cli:h9-plugin")
    session.metadata["task_profile"] = "h9"
    session.add_message("user", "first")
    await loop._run_agent_loop(
        [{"role": "user", "content": "first"}], runtime=loop.llm_runtime(), session=session,
        channel="cli", chat_id="h9-plugin", original_user_text="first",
    )
    session.add_message("user", "second")
    await loop._run_agent_loop(
        [{"role": "user", "content": "second"}], runtime=loop.llm_runtime(), session=session,
        channel="cli", chat_id="h9-plugin", original_user_text="second",
    )

    loop.plugin_manager.start_h9_selected_candidate.assert_called_once()


@pytest.mark.asyncio
async def test_h8_agent_loop_keeps_command_established_owner_across_profile_task_context(
    tmp_path,
) -> None:
    """A later Profile task id must not invalidate a command-created handoff."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.tools.context import RequestContext

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="done", tool_calls=[]))
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.get_definitions = MagicMock(return_value=[])
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    loop.loop_handoff_manager = manager
    session = loop.sessions.get_or_create("cli:h8-command-owner")

    session.add_message("user", "establish owner")
    await loop._run_agent_loop(
        [{"role": "user", "content": "establish owner"}],
        runtime=loop.llm_runtime(),
        session=session,
        channel="cli",
        chat_id="h8-command-owner",
        original_user_text="establish owner",
    )
    manager.request_switch(session.key, "plan-then-act", "1")
    original = manager.store.load(session.key)
    assert original is not None

    session.add_message("user", "continue with Profile")
    await loop._run_agent_loop(
        [{"role": "user", "content": "continue with Profile"}],
        runtime=loop.llm_runtime(),
        session=session,
        channel="cli",
        chat_id="h8-command-owner",
        original_user_text="continue with Profile",
        request_context=RequestContext(
            channel="cli",
            chat_id="h8-command-owner",
            session_key=session.key,
            runtime=loop.llm_runtime(),
            workspace=tmp_path,
            attributes={
                "devpilot_task_id": "profile-created-task",
                "devpilot_project_id": "profile-project",
            },
        ),
    )

    _driver, record = manager.active_driver(session.key)
    assert record.status == "active"
    assert record.task_id == original.task_id
    assert record.project_id == original.project_id


@pytest.mark.asyncio
async def test_h8_profile_command_history_does_not_pause_the_first_model_turn(tmp_path) -> None:
    """Filtered command history must not look stale to a newly durable Loop owner."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.agent.task_profiles import TaskProfile, TaskProfileCatalog

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="done", tool_calls=[]))
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.loop_handoff_manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    loop.task_profiles = TaskProfileCatalog(
        {
            "h10-profile": TaskProfile(
                name="h10-profile",
                skill_names=("my",),
                allowed_tools=frozenset(loop.tools.tool_names),
                output_contract=("complete",),
            )
        }
    )

    await loop.process_direct("/task-profile h10-profile", session_key="cli:h10-command-history")
    result = await loop.process_direct("use the available tool", session_key="cli:h10-command-history")

    assert result is not None
    assert result.content == "done"
    provider.chat_with_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_h8_switch_waits_for_tool_then_continues_same_runner(tmp_path) -> None:
    """A pending switch cannot replay a tool call, but affects the next model step."""
    from nanobot.agent.runner import AgentRunner

    process_id = os.getpid()
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
            "session-1",
            "task-1",
            "project-1",
            int(payload["iteration"]),
            len(checkpoints),
            3 - int(payload["iteration"]),
            pending_tool_calls=tuple(
                str(item["id"]) for item in payload.get("pending_tool_calls", [])
            ),
        )
        if not requested:
            manager.request_switch("session-1", "plan-then-act", "1")
            requested = True
        managed.note_checkpoint_committed(manager.commit_boundary("session-1", raw))

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
    record = manager.store.load("session-1")
    assert record is not None
    assert (record.session_id, record.task_id, record.project_id) == (
        "session-1",
        "task-1",
        "project-1",
    )
    assert os.getpid() == process_id
    assert managed.name == "plan-then-act"
    assert any(
        item.get("content", "").startswith("Host execution phase: act") for item in second_messages
    )


@pytest.mark.asyncio
async def test_h8_remote_strategy_uses_redacted_callback_and_host_validation() -> None:
    from nanobot.agent.runner import AgentRunner

    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="finished", tool_calls=[])
    )
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


def test_h8_durable_unsettled_checkpoint_pauses_when_session_snapshot_is_missing(tmp_path) -> None:
    from nanobot.agent.loop import AgentLoop

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    session = loop.sessions.get_or_create("cli:crash-before-session")
    durable = LoopCheckpoint(
        session.key,
        "task-1",
        "project-1",
        2,
        1,
        2,
        pending_tool_calls=("call-1",),
        uncertain_side_effect_refs=("run_id:run-1",),
    )
    manager.ensure(durable)
    loop.loop_handoff_manager = manager

    assert loop._restore_runtime_checkpoint(session) is True
    paused = session.metadata[loop._H7_HANDOFF_PAUSE_KEY]
    assert paused["reason"] == "session_checkpoint_missing"
    assert LoopCheckpoint.from_dict(paused["h7_loop_checkpoint"]) == durable


def test_h8_result_saved_before_handoff_checkpoint_pauses_instead_of_replaying(tmp_path) -> None:
    from nanobot.agent.loop import AgentLoop

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    manager = LoopHandoffManager(SqliteLoopHandoffStore(tmp_path / "devpilot.db"))
    session = loop.sessions.get_or_create("cli:result-before-checkpoint")
    session.add_message("user", "run")
    durable = LoopCheckpoint(session.key, "task-1", "project-1", 1, 1, 2)
    manager.ensure(durable)
    loop.loop_handoff_manager = manager
    session.metadata[loop._RUNTIME_CHECKPOINT_KEY] = {
        "phase": "tools_completed",
        "h7_loop_checkpoint": LoopCheckpoint(
            session.key,
            "task-1",
            "project-1",
            1,
            2,
            2,
            completed_tool_results=("call-1",),
        ).to_dict(),
        "completed_tool_results": [{"tool_call_id": "call-1", "content": "done"}],
        "pending_tool_calls": [],
    }

    assert loop._restore_runtime_checkpoint(session) is True
    assert session.metadata[loop._H7_HANDOFF_PAUSE_KEY]["reason"] == (
        "durable_session_checkpoint_mismatch"
    )


def test_h8_stateful_plan_switch_and_restart_preserves_completed_position(tmp_path) -> None:
    store = SqliteLoopHandoffStore(tmp_path / "devpilot.db")
    manager = LoopHandoffManager(store)
    checkpoint = LoopCheckpoint("session-1", "task-1", "project-1", 5, 9, 5)
    manager.ensure(checkpoint)
    plan = PlanThenActDriver(plan=("understand", "act", "verify", "review"))
    manager.request_switch(
        "session-1",
        plan.name,
        plan.version,
        driver_state=plan.export_state(),
    )
    manager.commit_boundary("session-1", checkpoint)

    restarted = ManagedLoopDriver(LoopHandoffManager(store), "session-1")
    action = restarted.next_action(StepInput("session-1", "task-1", "project-1", 5, 9, 99))
    assert action.reason == "plan_step:act"

    continued = LoopCheckpoint(
        "session-1",
        "task-1",
        "project-1",
        6,
        10,
        4,
        completed_tool_results=("call-1",),
    )
    manager.commit_boundary("session-1", continued)
    manager.request_switch("session-1", "default-react", "1")
    restored = manager.commit_boundary("session-1", continued)
    assert restored.driver_name == "default-react"
    assert restored.checkpoint.completed_tool_results == ("call-1",)


@pytest.mark.asyncio
async def test_h8_remote_projection_cannot_restore_durable_budget_after_restart(tmp_path) -> None:
    from nanobot.agent.runner import AgentRunner

    handoff = SqliteLoopHandoffStore(tmp_path / "devpilot.db")
    manager = LoopHandoffManager(handoff)
    checkpoint = LoopCheckpoint("session-1", "task-1", "project-1", 0, 0, 1)
    manager.ensure(checkpoint)
    strategy = RemoteStrategyDriver("strategy-one", "d" * 64)
    manager.request_switch(
        "session-1", strategy.name, strategy.version, driver_state=strategy.export_state()
    )
    manager.commit_boundary("session-1", checkpoint)
    managed = ManagedLoopDriver(LoopHandoffManager(handoff), "session-1")
    provider = MagicMock(spec=LLMProvider)
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="done", tool_calls=[]))
    tools = MagicMock()
    tools.get_definitions.return_value = []
    received: list[dict[str, object]] = []

    async def remote_decision(projection: dict[str, object]) -> dict[str, object]:
        received.append(projection)
        return {"schema": projection["schema"], "action": "request_model", "reason": "continue"}

    result = await AgentRunner().run(
        make_run_spec(
            provider,
            initial_messages=[{"role": "user", "content": "continue"}],
            tools=tools,
            model="test-model",
            max_iterations=5,
            max_tool_result_chars=4_000,
            loop_driver=managed,
            remote_decision_callback=remote_decision,
            session_key="session-1",
            task_id="task-1",
            project_id="project-1",
        )
    )

    assert result.final_content == "done"
    assert received and received[0]["remaining_iterations"] == 1
