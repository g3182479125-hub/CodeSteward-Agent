"""Stable decision contract for the live harness loop drivers.

Drivers decide *what happens next*.  The host remains the only owner of model
calls, tools, persistence, permissions, budgets and cancellation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol


class DriverActionKind(StrEnum):
    REQUEST_MODEL = "request_model"
    DISPATCH_TOOLS = "dispatch_tools"
    FINISH = "finish"
    PAUSE = "pause"


@dataclass(frozen=True, slots=True)
class StepInput:
    """Serializable host facts visible to a driver at one safe step boundary."""

    session_id: str
    task_id: str
    project_id: str
    step: int
    message_position: int
    remaining_iterations: int
    completed_tool_results: tuple[str, ...] = ()
    pending_tool_calls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DriverAction:
    """One host-validated decision; never an instruction to perform side effects."""

    kind: DriverActionKind
    reason: str = ""


class LoopDriver(Protocol):
    """Decision seam used by the host only at a committed step boundary."""

    name: str
    version: str

    def next_action(self, step: StepInput) -> DriverAction: ...


@dataclass(frozen=True, slots=True)
class DefaultReactDriver:
    """Preserves the existing request-model → tool-dispatch → response loop."""

    name: str = "default-react"
    version: str = "1"

    def next_action(self, step: StepInput) -> DriverAction:
        if step.pending_tool_calls:
            return DriverAction(DriverActionKind.DISPATCH_TOOLS, "pending_tools")
        if step.remaining_iterations <= 0:
            return DriverAction(DriverActionKind.PAUSE, "iteration_budget_exhausted")
        return DriverAction(DriverActionKind.REQUEST_MODEL, "next_model_step")


@dataclass(frozen=True, slots=True)
class LoopCheckpoint:
    """Public, JSON-safe handoff facts; never stores handles, tokens or coroutines."""

    session_id: str
    task_id: str
    project_id: str
    step: int
    message_position: int
    remaining_iterations: int
    completed_tool_results: tuple[str, ...] = ()
    pending_tool_calls: tuple[str, ...] = ()
    versions: Mapping[str, str] = field(default_factory=dict)
    business_references: Mapping[str, str] = field(default_factory=dict)
    uncertain_side_effect_refs: tuple[str, ...] = ()
    checkpoint_schema: str = "h7-v1"

    @property
    def switch_safe(self) -> bool:
        """Only a committed, fully-known boundary may change Driver ownership."""
        return not self.pending_tool_calls and not self.uncertain_side_effect_refs

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "LoopCheckpoint":
        if value.get("checkpoint_schema") != "h7-v1":
            raise ValueError("unsupported loop checkpoint schema")
        required = ("session_id", "task_id", "project_id", "step", "message_position", "remaining_iterations")
        if any(not isinstance(value.get(key), str if key.endswith("_id") else int) for key in required):
            raise ValueError("invalid loop checkpoint identity")
        return cls(
            session_id=str(value["session_id"]), task_id=str(value["task_id"]), project_id=str(value["project_id"]),
            step=int(value["step"]), message_position=int(value["message_position"]), remaining_iterations=int(value["remaining_iterations"]),
            completed_tool_results=tuple(str(item) for item in value.get("completed_tool_results", ())),
            pending_tool_calls=tuple(str(item) for item in value.get("pending_tool_calls", ())),
            versions={str(key): str(item) for key, item in dict(value.get("versions", {})).items()},
            business_references={str(key): str(item) for key, item in dict(value.get("business_references", {})).items()},
            uncertain_side_effect_refs=tuple(
                str(item) for item in value.get("uncertain_side_effect_refs", ())
            ),
        )
