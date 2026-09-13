"""Redacted H8 projection and response validation for remote loop strategies.

The remote candidate never receives a session transcript, provider state,
approval capability, tool arguments, filesystem location, or business IDs.
It can only choose among Host-defined next decisions for a bounded step.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from nanobot.agent.loop_driver import DriverAction, DriverActionKind, StepInput

_SCHEMA = "h8-remote-driver-projection-v1"
_ALLOWED_ACTIONS = frozenset({"request_model", "pause", "finish"})


def build_remote_projection(step: StepInput) -> dict[str, object]:
    """Return the smallest fixed-shape input a remote strategy may inspect."""
    return {
        "schema": _SCHEMA,
        "task_ref": _opaque_ref(step.task_id),
        "project_ref": _opaque_ref(step.project_id),
        "step": step.step,
        "remaining_iterations": step.remaining_iterations,
        "completed_result_count": len(step.completed_tool_results),
        "pending_tool_count": len(step.pending_tool_calls),
        "allowed_actions": sorted(_ALLOWED_ACTIONS),
    }


def validate_remote_decision(value: Mapping[str, object], step: StepInput) -> DriverAction:
    """Translate a pure remote decision into a Host-owned action.

    A remote strategy cannot request tool dispatch, alter the budget, attach
    data, or direct a side effect.  Pending tools remain a Host decision.
    """
    if value.get("schema") != _SCHEMA:
        raise ValueError("remote driver response schema is unsupported")
    action = value.get("action")
    if not isinstance(action, str) or action not in _ALLOWED_ACTIONS:
        raise ValueError("remote driver action is not allowed")
    if step.pending_tool_calls and action == "request_model":
        raise ValueError("remote driver cannot skip pending tool settlement")
    reason = value.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 160:
        raise ValueError("remote driver reason is invalid")
    return DriverAction(DriverActionKind(action), reason)


def _opaque_ref(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
