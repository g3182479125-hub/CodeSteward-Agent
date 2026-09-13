"""Stable decision contract for the live harness loop drivers.

Drivers decide *what happens next*.  The host remains the only owner of model
calls, tools, persistence, permissions, budgets and cancellation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol

_BUSINESS_REFERENCE_KEYS = (
    "operation_id",
    "run_id",
    "change_set_id",
    "sandbox_id",
    "approval_id",
)
_UNCERTAIN_STATUSES = frozenset({"unknown", "pending", "running", "in_progress"})


class DriverActionKind(StrEnum):
    REQUEST_MODEL = "request_model"
    DISPATCH_TOOLS = "dispatch_tools"
    FINISH = "finish"
    PAUSE = "pause"
    REQUEST_REMOTE_DECISION = "request_remote_decision"


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

    def export_state(self) -> Mapping[str, object]: ...

    @classmethod
    def restore_state(cls, state: Mapping[str, object]) -> "LoopDriver": ...


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

    def export_state(self) -> Mapping[str, object]:
        """Return an immutable, serializable driver artifact."""
        return {"driver_state_schema": "default-react-v1"}

    @classmethod
    def restore_state(cls, state: Mapping[str, object]) -> "DefaultReactDriver":
        if state != {"driver_state_schema": "default-react-v1"}:
            raise ValueError("incompatible default-react driver state")
        return cls()


@dataclass(frozen=True, slots=True)
class PlanThenActDriver:
    """A deliberately finite planning driver that still uses the one Host executor.

    The plan is an execution *view* only.  It cannot directly call a model,
    tool, filesystem, or sandbox; the Host validates and performs each action.
    """

    plan: tuple[str, ...] = ("understand", "act", "verify")
    name: str = "plan-then-act"
    version: str = "1"

    def __post_init__(self) -> None:
        if not self.plan or any(not isinstance(item, str) or not item for item in self.plan):
            raise ValueError("plan-then-act requires non-empty structured plan steps")

    def next_action(self, step: StepInput) -> DriverAction:
        if step.pending_tool_calls:
            return DriverAction(DriverActionKind.DISPATCH_TOOLS, "plan_pending_tools")
        if step.remaining_iterations <= 0:
            return DriverAction(DriverActionKind.PAUSE, "plan_iteration_budget_exhausted")
        phase = self.plan[step.step % len(self.plan)]
        return DriverAction(DriverActionKind.REQUEST_MODEL, f"plan_step:{phase}")

    def export_state(self) -> Mapping[str, object]:
        return {
            "driver_state_schema": "plan-then-act-v1",
            "plan": list(self.plan),
        }

    @classmethod
    def restore_state(cls, state: Mapping[str, object]) -> "PlanThenActDriver":
        if state.get("driver_state_schema") != "plan-then-act-v1":
            raise ValueError("incompatible plan-then-act driver state")
        plan = state.get("plan")
        if not isinstance(plan, list) or any(not isinstance(item, str) for item in plan):
            raise ValueError("invalid plan-then-act plan")
        return cls(plan=tuple(plan))


@dataclass(frozen=True, slots=True)
class RemoteStrategyDriver:
    """An immutable remote strategy artifact with no local execution power."""

    strategy_id: str
    source_hash: str
    name: str = "remote-strategy"
    version: str = "1"

    def __post_init__(self) -> None:
        if not self.strategy_id or len(self.strategy_id) > 64:
            raise ValueError("remote strategy identifier is invalid")
        if len(self.source_hash) != 64 or any(char not in "0123456789abcdef" for char in self.source_hash):
            raise ValueError("remote strategy source hash is invalid")

    def next_action(self, step: StepInput) -> DriverAction:
        if step.pending_tool_calls:
            return DriverAction(DriverActionKind.DISPATCH_TOOLS, "remote_pending_tools")
        if step.remaining_iterations <= 0:
            return DriverAction(DriverActionKind.PAUSE, "remote_iteration_budget_exhausted")
        return DriverAction(DriverActionKind.REQUEST_REMOTE_DECISION, "remote_strategy_decision")

    def export_state(self) -> Mapping[str, object]:
        return {
            "driver_state_schema": "remote-strategy-v1",
            "strategy_id": self.strategy_id,
            "source_hash": self.source_hash,
        }

    @classmethod
    def restore_state(cls, state: Mapping[str, object]) -> "RemoteStrategyDriver":
        if state.get("driver_state_schema") != "remote-strategy-v1":
            raise ValueError("incompatible remote strategy state")
        strategy_id = state.get("strategy_id")
        source_hash = state.get("source_hash")
        if not isinstance(strategy_id, str) or not isinstance(source_hash, str):
            raise ValueError("invalid remote strategy state")
        return cls(strategy_id=strategy_id, source_hash=str(source_hash))


def merge_business_facts(
    references: Mapping[str, str],
    uncertain_refs: Iterable[str],
    completed_results: Iterable[object],
    pending_calls: Iterable[object],
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Accumulate named business facts without storing arbitrary tool output."""
    merged = dict(references)
    uncertain = set(uncertain_refs)

    def record(value: object) -> None:
        if isinstance(value, str):
            try:
                record(json.loads(value))
            except json.JSONDecodeError:
                return
            return
        if isinstance(value, list):
            for item in value:
                record(item)
            return
        if not isinstance(value, Mapping):
            return
        identifiers = {
            key: item
            for key in _BUSINESS_REFERENCE_KEYS
            if isinstance((item := value.get(key)), str) and item
        }
        for key, item in identifiers.items():
            merged[f"{key}:{item[:256]}"] = item[:256]
        status = value.get("status")
        if isinstance(status, str) and status.lower() in _UNCERTAIN_STATUSES:
            for key, item in identifiers.items():
                uncertain.add(f"{key}:{item[:256]}")
        for item in value.values():
            record(item)

    for result in completed_results:
        if isinstance(result, Mapping) and isinstance(result.get("tool_call_id"), str):
            uncertain.discard(f"tool_call:{result['tool_call_id'][:256]}")
        record(result.get("content") if isinstance(result, Mapping) else result)
    for call in pending_calls:
        if isinstance(call, Mapping) and isinstance(call.get("id"), str):
            uncertain.add(f"tool_call:{call['id'][:256]}")
    return merged, tuple(sorted(uncertain))


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

    _MAX_SERIALIZED_BYTES = 64 * 1024

    @property
    def switch_safe(self) -> bool:
        """Only a committed, fully-known boundary may change Driver ownership."""
        return not self.pending_tool_calls and not self.uncertain_side_effect_refs

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self._MAX_SERIALIZED_BYTES:
            raise ValueError("loop checkpoint exceeds 64 KiB")
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "LoopCheckpoint":
        if value.get("checkpoint_schema") != "h7-v1":
            raise ValueError("unsupported loop checkpoint schema")
        required = ("session_id", "task_id", "project_id", "step", "message_position", "remaining_iterations")
        if any(not isinstance(value.get(key), str if key.endswith("_id") else int) for key in required):
            raise ValueError("invalid loop checkpoint identity")
        versions = value.get("versions", {})
        business_references = value.get("business_references", {})
        collections = (
            value.get("completed_tool_results", ()),
            value.get("pending_tool_calls", ()),
            value.get("uncertain_side_effect_refs", ()),
        )
        if (
            not isinstance(versions, Mapping)
            or not isinstance(business_references, Mapping)
            or any(not isinstance(items, (list, tuple)) for items in collections)
            or any(not isinstance(item, str) for items in collections for item in items)
            or any(
                not isinstance(key, str) or not isinstance(item, str)
                for mapping in (versions, business_references)
                for key, item in mapping.items()
            )
        ):
            raise ValueError("invalid loop checkpoint state")
        checkpoint = cls(
            session_id=str(value["session_id"]), task_id=str(value["task_id"]), project_id=str(value["project_id"]),
            step=int(value["step"]), message_position=int(value["message_position"]), remaining_iterations=int(value["remaining_iterations"]),
            completed_tool_results=tuple(value.get("completed_tool_results", ())),
            pending_tool_calls=tuple(value.get("pending_tool_calls", ())),
            versions=dict(versions),
            business_references=dict(business_references),
            uncertain_side_effect_refs=tuple(value.get("uncertain_side_effect_refs", ())),
        )
        checkpoint.to_dict()
        return checkpoint
