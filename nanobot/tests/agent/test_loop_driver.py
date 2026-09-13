from __future__ import annotations

import pytest

from nanobot.agent.loop_driver import (
    DefaultReactDriver,
    DriverActionKind,
    LoopCheckpoint,
    StepInput,
)


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


def test_checkpoint_rejects_unknown_schema() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        LoopCheckpoint.from_dict({"checkpoint_schema": "unknown"})
