"""Stable, user-facing outcomes for one agent turn.

The runner and loop use several internal stop reasons.  This module is the
single boundary that turns those implementation details into a safe outcome
that can be persisted, projected, and shown to a user.  In particular, a
guard stopping one turn is not the same thing as pausing an entire session.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

OutcomeCategory = Literal[
    "completed",
    "recoverable_failure",
    "guard_stopped",
    "blocked_for_safety",
    "unknown_side_effect",
    "cancelled",
]
OutcomeScope = Literal["turn", "operation", "project"]
RetryHint = Literal["retry_new", "check_status", "change_input", "none"]
Continuation = Literal["allow", "allow_readonly", "query_only"]

TURN_OUTCOME_METADATA_KEY = "latest_turn_outcome"
TURN_OUTCOME_SCHEMA_VERSION = 1
_SAFE_OPERATION_REF = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


_MESSAGES: dict[str, str] = {
    "turn.completed": "这次处理完成了。",
    "guard.no_progress": "这一步一直没有新进展，我先停在这里。你可以补充线索、换个问法，或者继续聊别的。",
    "safety.path_rejected": "这个目录不在当前授权范围内，所以我没有读取它。你可以换一个已授权的路径。",
    "operation.unknown_result": "上次执行结果还不能确认；我不会重复执行，但可以先查状态。",
    "tool.failed": "这个操作没有完成，但不影响后续对话。你可以缩小范围后重试，或者继续聊别的。",
    "devpilot.failed": "项目操作没有完成，但不影响后续对话。你可以检查项目路径后重试，或者继续聊别的。",
    "subagent.failed": "辅助任务没有完成，但不影响主对话。你可以换个策略，或者继续聊别的。",
    "model.failed": "这次请求没有完成，不影响后续对话。你可以重试，或者先继续其他问题。",
    "turn.empty": "这次没有得到完整回答，但不影响后续对话。你可以换个问法再试。",
    "turn.cancelled": "这次操作已停止，不影响后续对话。",
}


@dataclass(frozen=True)
class TurnOutcome:
    """A safe and serializable result of one turn."""

    category: OutcomeCategory
    scope: OutcomeScope
    user_message_key: str
    retry_hint: RetryHint
    continuation: Continuation
    operation_refs: tuple[str, ...] = ()
    internal_code: str | None = None

    @property
    def user_message(self) -> str:
        return _MESSAGES[self.user_message_key]

    def to_metadata(self) -> dict[str, Any]:
        """Return the redacted projection safe for session/WebSocket metadata."""
        return {
            "schema_version": TURN_OUTCOME_SCHEMA_VERSION,
            "category": self.category,
            "scope": self.scope,
            "message_key": self.user_message_key,
            "retry_hint": self.retry_hint,
            "continuation": self.continuation,
            "operation_refs": [
                ref for ref in self.operation_refs if _SAFE_OPERATION_REF.fullmatch(ref)
            ],
        }


def message_for_outcome(outcome: TurnOutcome) -> str:
    """Return only catalog text; internal stop reasons never reach the user."""
    return outcome.user_message


def should_replace_content(
    outcome: TurnOutcome,
    stop_reason: str,
    content: str | None,
) -> bool:
    """Decide whether an internal runner string must be replaced for the user.

    A max-iteration turn may contain a useful partial answer.  Preserve that
    answer; replace only empty content or known internal error/pause text.
    """
    if outcome.category == "completed":
        return False
    if not content or not content.strip():
        return True
    reason = (stop_reason or "").strip().lower()
    if reason in {"driver_paused", "tool_error", "error", "model_error", "provider_error"}:
        return True
    lowered = content.lower()
    return any(
        marker in lowered
        for marker in (
            "task paused",
            "pathboundaryerror",
            "diagnostic-branch budget",
            "uncertain_side_effect_requires_recovery",
            "sorry, i encountered an error",
        )
    )


def classify_turn_outcome(
    stop_reason: str,
    detail: str | None = None,
    *,
    operation_refs: Sequence[str] = (),
) -> TurnOutcome:
    """Map internal runner facts to the stable turn contract."""
    reason = (stop_reason or "").strip().lower()
    text = (detail or "").strip().lower()
    refs = tuple(ref for ref in operation_refs if isinstance(ref, str) and ref)

    if reason in {"completed", "stop", "driver_finished", ""}:
        return TurnOutcome("completed", "turn", "turn.completed", "none", "allow")
    if reason in {"cancelled", "canceled", "cancel"}:
        return TurnOutcome("cancelled", "turn", "turn.cancelled", "none", "allow")

    if any(
        marker in text
        for marker in (
            "pathboundaryerror",
            "outside the allowed roots",
            "outside the allowed root",
            "outside configured workspace",
            "outside allowed directory",
            "workspace scope rejected",
        )
    ):
        return TurnOutcome(
            "blocked_for_safety",
            "turn",
            "safety.path_rejected",
            "change_input",
            "allow",
            internal_code="path_boundary",
        )

    if any(
        marker in text
        for marker in (
            "uncertain_side_effect_requires_recovery",
            "unknown side effect",
            "result is unknown",
            "result cannot be confirmed",
        )
    ):
        return TurnOutcome(
            "unknown_side_effect",
            "operation",
            "operation.unknown_result",
            "check_status",
            "allow_readonly",
            refs,
            internal_code="unknown_side_effect",
        )

    if refs and reason in {"driver_paused", "guard_stopped"}:
        return TurnOutcome(
            "unknown_side_effect",
            "operation",
            "operation.unknown_result",
            "check_status",
            "allow_readonly",
            refs,
            internal_code="unknown_side_effect",
        )

    if reason in {"driver_paused", "max_iterations", "guard_stopped"} or any(
        marker in text
        for marker in (
            "diagnostic-branch budget",
            "bounded diagnostic",
            "no progress",
            "iteration budget",
        )
    ):
        return TurnOutcome(
            "guard_stopped",
            "turn",
            "guard.no_progress",
            "change_input",
            "allow",
            internal_code="guard_stopped",
        )

    if reason == "subagent_error":
        return TurnOutcome(
            "recoverable_failure",
            "turn",
            "subagent.failed",
            "retry_new",
            "allow",
            internal_code=reason,
        )
    if reason in {"devpilot_error", "task_profile_error", "plugin_error"}:
        return TurnOutcome(
            "recoverable_failure",
            "turn",
            "devpilot.failed",
            "retry_new",
            "allow",
            internal_code=reason,
        )
    if reason == "tool_error":
        return TurnOutcome(
            "recoverable_failure",
            "turn",
            "tool.failed",
            "retry_new",
            "allow",
            internal_code=reason,
        )
    if reason in {"error", "model_error", "provider_error"}:
        return TurnOutcome(
            "recoverable_failure",
            "turn",
            "model.failed",
            "retry_new",
            "allow",
            internal_code=reason,
        )
    if reason == "empty_final_response":
        return TurnOutcome(
            "recoverable_failure",
            "turn",
            "turn.empty",
            "change_input",
            "allow",
            internal_code=reason,
        )

    return TurnOutcome(
        "recoverable_failure",
        "turn",
        "tool.failed",
        "retry_new",
        "allow",
        internal_code=reason or "unknown",
    )


def classify_exception(exc: BaseException, *, sender_id: str | None = None) -> str:
    """Choose a coarse failure source without exposing exception details."""
    module = type(exc).__module__.lower()
    haystack = f"{module} {type(exc).__name__} {exc}".lower()
    if sender_id == "subagent" or "subagent" in haystack:
        return "subagent_error"
    if any(
        marker in haystack
        for marker in ("devpilot", "taskprofile", "task profile", "plugin")
    ):
        return (
            "task_profile_error"
            if "taskprofile" in haystack or "task profile" in haystack
            else "devpilot_error"
        )
    if any(marker in haystack for marker in ("provider", "model", "llm", "inference")):
        return "model_error"
    return "tool_error"
