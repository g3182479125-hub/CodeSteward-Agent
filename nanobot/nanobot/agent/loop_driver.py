"""Stable decision contract for the live harness loop drivers.

Drivers decide *what happens next*.  The host remains the only owner of model
calls, tools, persistence, permissions, budgets and cancellation.
"""

from __future__ import annotations

import hashlib
import json
import re
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
_SETTLED_STATUSES = frozenset({"completed", "failed", "cancelled", "rejected", "not_found"})


def tool_call_fingerprint(name: str, arguments: object) -> str:
    """Hash one tool proposal without persisting its arguments in H8 state."""
    normalized_arguments = arguments
    if isinstance(arguments, str):
        try:
            normalized_arguments = json.loads(arguments)
        except (TypeError, ValueError):
            normalized_arguments = arguments
    payload = json.dumps(
        {"name": name, "arguments": normalized_arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


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
    strategy_version: str = "1.0.0"
    name: str = "remote-strategy"
    version: str = "1"

    def __post_init__(self) -> None:
        if not self.strategy_id or len(self.strategy_id) > 64:
            raise ValueError("remote strategy identifier is invalid")
        if len(self.source_hash) != 64 or any(char not in "0123456789abcdef" for char in self.source_hash):
            raise ValueError("remote strategy source hash is invalid")
        if not self.strategy_version or len(self.strategy_version) > 64:
            raise ValueError("remote strategy version is invalid")

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
            "strategy_version": self.strategy_version,
        }

    @classmethod
    def restore_state(cls, state: Mapping[str, object]) -> "RemoteStrategyDriver":
        if state.get("driver_state_schema") != "remote-strategy-v1":
            raise ValueError("incompatible remote strategy state")
        strategy_id = state.get("strategy_id")
        source_hash = state.get("source_hash")
        strategy_version = state.get("strategy_version")
        if (
            not isinstance(strategy_id, str)
            or not isinstance(source_hash, str)
            or not isinstance(strategy_version, str)
        ):
            raise ValueError("invalid remote strategy state")
        return cls(
            strategy_id=strategy_id,
            source_hash=str(source_hash),
            strategy_version=strategy_version,
        )


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
        if isinstance(status, str):
            normalized_status = status.lower()
            if normalized_status in _UNCERTAIN_STATUSES:
                for key, item in identifiers.items():
                    uncertain.add(f"{key}:{item[:256]}")
            elif normalized_status in _SETTLED_STATUSES:
                settled_refs = {
                    f"{key}:{item[:256]}" for key, item in identifiers.items()
                }
                settles_existing_operation = bool(settled_refs & uncertain)
                for key, item in identifiers.items():
                    uncertain.discard(f"{key}:{item[:256]}")
                if settles_existing_operation:
                    # A matching terminal recovery result settles the stale
                    # in-process intent that led to this business operation
                    # as well. Any tool calls in the current checkpoint are
                    # re-added below, so this only retires older intents.
                    for ref in tuple(uncertain):
                        if ref.startswith("tool_call:"):
                            uncertain.discard(ref)
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
    session_prefix_hash: str = ""
    completed_tool_results: tuple[str, ...] = ()
    pending_tool_calls: tuple[str, ...] = ()
    versions: Mapping[str, str] = field(default_factory=dict)
    business_references: Mapping[str, str] = field(default_factory=dict)
    uncertain_side_effect_refs: tuple[str, ...] = ()
    checkpoint_schema: str = "h7-v1"
    blocked_tool_call_fingerprints: tuple[str, ...] = ()

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
        session_prefix_hash = value.get("session_prefix_hash", "")
        collections = (
            value.get("completed_tool_results", ()),
            value.get("pending_tool_calls", ()),
            value.get("uncertain_side_effect_refs", ()),
            value.get("blocked_tool_call_fingerprints", ()),
        )
        if (
            not isinstance(versions, Mapping)
            or not isinstance(business_references, Mapping)
            or not isinstance(session_prefix_hash, str)
            or (session_prefix_hash and not re.fullmatch(r"[0-9a-f]{64}", session_prefix_hash))
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
            session_prefix_hash=session_prefix_hash,
            completed_tool_results=tuple(value.get("completed_tool_results", ())),
            pending_tool_calls=tuple(value.get("pending_tool_calls", ())),
            versions=dict(versions),
            business_references=dict(business_references),
            uncertain_side_effect_refs=tuple(value.get("uncertain_side_effect_refs", ())),
            blocked_tool_call_fingerprints=tuple(
                value.get("blocked_tool_call_fingerprints", ())
            ),
        )
        checkpoint.to_dict()
        return checkpoint


def session_message_prefix_hash(messages: Iterable[Mapping[str, object]]) -> str:
    """Hash the committed JSON-safe message prefix without hashing private metadata."""

    def _non_serializable_value(value: object) -> Mapping[str, str]:
        """Keep the hash operation fail-closed without serializing opaque objects.

        Production session messages are JSON values.  A provider/test double can
        still accidentally place an opaque object in a transient message before
        the normal session writer rejects it.  H8 must not turn that existing
        invalid value into a crash during checkpointing, or a retry could repeat
        a completed action.  Hash its stable type marker rather than its repr,
        which may contain memory addresses or private data.
        """

        value_type = type(value)
        return {
            "__nanobot_opaque_value__": (
                f"{value_type.__module__}.{value_type.__qualname__}"
            )
        }

    digest = hashlib.sha256()
    for message in messages:
        digest.update(
            json.dumps(
                message,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=_non_serializable_value,
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()
