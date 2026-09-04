"""Translate DevPilot run evidence into safe nanobot chat updates.

The control plane remains the source of truth.  This module is deliberately a
small projection layer: it accepts already-redacted Run/SSE records, keeps only
allow-listed fields, and never forwards paths, command output, credentials, or
capability tokens to the nanobot conversation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from .redaction import redact_sensitive_text

MAX_EVENTS = 64
MAX_MESSAGE_CHARS = 400

_SAFE_EVENT_TYPES = frozenset(
    {
        "run.started",
        "plan.created",
        "changeset.previewed",
        "changeset.approval_requested",
        "changeset.approval_issued",
        "changeset.applying",
        "changeset.applied",
        "changeset.verification_started",
        "changeset.verification_completed",
        "changeset.verification_failed",
        "execution.capability_denied",
        "execution.completed",
        "repair.round_started",
        "repair.stopped",
        "run.completed",
        "run.failed",
        "changeset.reverted",
        "changeset.apply_failed",
        "delivery.accepted",
        "delivery.stopped",
    }
)

_SAFE_PAYLOAD_KEYS = frozenset(
    {
        "status",
        "code",
        "capability",
        "profile",
        "command_profile",
        "backend",
        "region",
        "template",
        "exit_code",
        "duration_ms",
        "files",
        "file_count",
        "artifact_id",
        "artifact_ref",
        "round",
        "max_rounds",
        "count",
    }
)
_SAFE_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_SAFE_LABEL_KEYS = frozenset(
    {
        "run_id",
        "status",
        "code",
        "capability",
        "profile",
        "command_profile",
        "backend",
        "region",
        "template",
        "artifact_id",
        "artifact_ref",
    }
)
_SAFE_TIMESTAMP_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9T:+.\-Z]{0,63}$")


def _string(value: Any, *, limit: int = 160) -> str | None:
    if isinstance(value, str) and value:
        return value[:limit]
    return None


def _safe_detail_string(key: str, value: str) -> str | None:
    """Keep only bounded labels; reject paths and credential-shaped values."""
    value = redact_sensitive_text(value[:160])
    if value == "[REDACTED]" or "[REDACTED]" in value:
        return None
    if key in _SAFE_LABEL_KEYS and _SAFE_LABEL_PATTERN.fullmatch(value) is None:
        return None
    if key == "created_at" and _SAFE_TIMESTAMP_PATTERN.fullmatch(value) is None:
        return None
    return value


def _safe_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    mapping = cast(Mapping[str, Any], payload)
    result: dict[str, Any] = {}
    for key in _SAFE_PAYLOAD_KEYS:
        value = mapping.get(key)
        if isinstance(value, (str, int, float, bool)):
            if isinstance(value, str):
                if key == "files":
                    continue
                value = _safe_detail_string(key, value)
                if value is None:
                    continue
            result[key] = value
        elif key in {"files", "file_count", "round", "max_rounds", "count"}:
            # A file list is never forwarded; only a bounded count is useful
            # to the operator and safe for the model to see.
            if isinstance(value, (list, tuple, set)):
                sized = cast(Sequence[Any], value)
                result["file_count"] = min(len(sized), 12)
    return {key: result[key] for key in sorted(result)}


@dataclass(frozen=True)
class NanobotRunEvent:
    """One safe, model-visible projection of a persisted DevPilot event."""

    event_id: int
    sequence: int
    event_type: str
    created_at: str
    message: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "type": self.event_type,
            "created_at": self.created_at,
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True)
class NanobotRunProjection:
    """Safe chat payload returned by the nanobot adapter."""

    run_id: str
    status: str
    events: tuple[NanobotRunEvent, ...]
    last_event_id: int | None
    fallback: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "devpilot.run",
            "run_id": self.run_id,
            "status": self.status,
            "events": [item.to_dict() for item in self.events],
            "last_event_id": self.last_event_id,
            "fallback": self.fallback,
            "next_step": _next_step(self.status),
        }


def _next_step(status: str) -> str:
    if status == "completed":
        return "Run 已完成；如需交付结果，请读取 Artifact 或 delivery report。"
    if status == "failed":
        return "Run 已失败；请先查看错误码和最后阶段，不要把失败当作成功。"
    if status == "running":
        return "Run 仍在进行；可稍后用相同 run_id 和 last_event_id 继续读取。"
    return "Run 状态已记录；请以控制面返回的状态为准。"


def _event_message(event_type: str, details: Mapping[str, Any]) -> str:
    code = _string(details.get("code"))
    if event_type == "run.started":
        return "DevPilot Run 已开始。"
    if event_type == "run.completed":
        return "DevPilot Run 已完成。"
    if event_type == "run.failed":
        return f"DevPilot Run 失败{f'（{code}）' if code else ''}。"
    if event_type.endswith("approval_requested"):
        return "已记录用户确认请求，等待受信任审批动作。"
    if event_type.endswith("approval_issued"):
        return "控制面已签发一次性能力，后续动作仍需精确绑定。"
    if event_type.endswith("applying"):
        return "正在应用已批准的 ChangeSet。"
    if event_type.endswith("applied"):
        return "ChangeSet 已应用，等待验证或交付决策。"
    if event_type.endswith("verification_completed"):
        exit_code = details.get("exit_code")
        return f"验证已结束（exit_code={exit_code}）。"
    if event_type.endswith("verification_failed"):
        return f"验证失败{f'（{code}）' if code else ''}。"
    if event_type.endswith("capability_denied"):
        return f"执行能力不可用，已拒绝执行{f'（{code}）' if code else ''}。"
    if event_type.endswith("reverted"):
        return "ChangeSet 已回滚。"
    if event_type.endswith("apply_failed"):
        return f"应用失败{f'（{code}）' if code else ''}。"
    return f"DevPilot 阶段：{event_type}。"


def project_run_state(
    state: Mapping[str, Any], *, fallback: bool = False, max_events: int = MAX_EVENTS
) -> NanobotRunProjection:
    """Build a deterministic safe projection from ``GET /runs/{id}`."""
    run_id_value = _string(state.get("run_id"), limit=128)
    run_id = (
        _safe_detail_string("run_id", run_id_value)
        if run_id_value is not None
        else None
    )
    if run_id is None:
        raise ValueError("run_id is required")
    status_value = _string(state.get("status"), limit=32)
    status = (
        _safe_detail_string("status", status_value) if status_value is not None else None
    ) or "unknown"
    raw_events = state.get("events")
    if not isinstance(raw_events, list):
        raw_events = []
    raw_events = cast(list[Any], raw_events)
    events: list[NanobotRunEvent] = []
    for raw in raw_events[-max_events:]:
        event = _project_event(raw)
        if event is not None:
            events.append(event)
    return NanobotRunProjection(
        run_id=run_id,
        status=status,
        events=tuple(events),
        last_event_id=events[-1].event_id if events else None,
        fallback=fallback,
    )


def project_sse_events(
    *, run_id: str, status: str, events: Sequence[Mapping[str, Any]], fallback: bool = False
) -> NanobotRunProjection:
    """Build a safe projection from finite SSE records, preserving order."""
    state = {"run_id": run_id, "status": status, "events": events}
    return project_run_state(state, fallback=fallback)


def _project_event(raw: Any) -> NanobotRunEvent | None:
    if not isinstance(raw, Mapping):
        return None
    mapping = cast(Mapping[str, Any], raw)
    event_type = _string(mapping.get("type") or mapping.get("event_type"), limit=80)
    if event_type is None or event_type not in _SAFE_EVENT_TYPES:
        return None
    event_id_value = mapping.get("event_id")
    sequence_value = mapping.get("sequence")
    if not isinstance(event_id_value, (int, str)) or not isinstance(
        sequence_value, (int, str)
    ):
        return None
    try:
        event_id = int(event_id_value)
        sequence = int(sequence_value)
    except (TypeError, ValueError):
        return None
    if event_id < 0 or sequence < 1:
        return None
    created_at_value = _string(mapping.get("created_at"), limit=64)
    created_at = (
        _safe_detail_string("created_at", created_at_value)
        if created_at_value is not None
        else None
    ) or ""
    details = _safe_payload(mapping.get("redacted_payload", mapping.get("payload")))
    message = _event_message(event_type, details)[:MAX_MESSAGE_CHARS]
    return NanobotRunEvent(
        event_id=event_id,
        sequence=sequence,
        event_type=event_type,
        created_at=created_at,
        message=message,
        details=details,
    )
