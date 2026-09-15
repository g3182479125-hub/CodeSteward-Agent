"""Session metadata helpers for explicit sustained goals.

Tools set ``metadata[GOAL_STATE_KEY]``. Reads accept the legacy session key ``thread_goal``
for older sessions. Callers use ``goal_state_runtime_lines``, ``goal_state_ws_blob``, and
``runner_wall_llm_timeout_s`` and ``webui_interactive_llm_timeout_s`` without importing
tool implementations.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Mapping, MutableMapping, cast

from nanobot.cron.session_turns import is_cron_turn
from nanobot.session.manager import SessionManager

GOAL_STATE_KEY = "goal_state"
GOAL_COMMAND = "/goal"
MAX_GOAL_OBJECTIVE_CHARS = 4000
# Older builds stored the same JSON blob under this key.
_LEGACY_GOAL_STATE_SESSION_KEY = "thread_goal"
_MAX_OBJECTIVE_WS = 600
DEFAULT_WEBUI_INTERACTIVE_LLM_TIMEOUT_S = 75.0
_WEBUI_INTERACTIVE_LLM_TIMEOUT_ENV = "NANOBOT_WEBUI_LLM_TIMEOUT_S"


def _session_goal_raw(metadata: Mapping[str, Any] | None) -> Any:
    if not metadata:
        return None
    if GOAL_STATE_KEY in metadata:
        return metadata.get(GOAL_STATE_KEY)
    return metadata.get(_LEGACY_GOAL_STATE_SESSION_KEY)


def discard_legacy_goal_state_key(metadata: MutableMapping[str, Any]) -> None:
    """Remove legacy metadata key after migrating writes to :data:`GOAL_STATE_KEY`."""
    metadata.pop(_LEGACY_GOAL_STATE_SESSION_KEY, None)


def goal_state_raw(metadata: Mapping[str, Any] | None) -> Any:
    """Return the session goal blob under :data:`GOAL_STATE_KEY` or the legacy key."""
    return _session_goal_raw(metadata)


def sustained_goal_active(metadata: Mapping[str, Any] | None) -> bool:
    """True when this session has an active sustained objective."""
    goal = parse_goal_state(goal_state_raw(metadata))
    return isinstance(goal, dict) and goal.get("status") == "active"


def explicit_goal_requested(message_metadata: Mapping[str, Any] | None) -> bool:
    """True when this turn was explicitly started by the ``/goal`` command."""
    if not message_metadata:
        return False
    if message_metadata.get("goal_requested") is True:
        return True
    return str(message_metadata.get("original_command") or "").strip() == GOAL_COMMAND


def sustained_goal_turn(
    metadata: Mapping[str, Any] | None,
    *,
    message_metadata: Mapping[str, Any] | None = None,
) -> bool:
    """True when this turn should use sustained-goal runtime limits."""
    return sustained_goal_active(metadata) or explicit_goal_requested(message_metadata)


def parse_goal_state(blob: Any) -> dict[str, Any] | None:
    if blob is None:
        return None
    if isinstance(blob, dict):
        return cast(dict[str, Any], blob)
    if isinstance(blob, str):
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            return None
        return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else None
    return None


def goal_state_runtime_lines(metadata: Mapping[str, Any] | None) -> list[str]:
    """Lines appended inside the Runtime Context block when a goal is active."""
    if not metadata:
        return []
    goal = parse_goal_state(_session_goal_raw(metadata))
    if not isinstance(goal, dict) or goal.get("status") != "active":
        return []
    objective = str(goal.get("objective") or "").strip()
    if not objective:
        return ["Goal: active (no objective text stored)."]
    if len(objective) > MAX_GOAL_OBJECTIVE_CHARS:
        objective = objective[:MAX_GOAL_OBJECTIVE_CHARS].rstrip() + "\n… (truncated)"
    out = ["Goal (active):", objective]
    hint = str(goal.get("ui_summary") or "").strip()
    if hint:
        out.append(f"Summary: {hint}")
    return out


def goal_state_ws_blob(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """JSON-safe snapshot for WebSocket ``goal_state`` events (one chat_id per frame)."""
    goal = parse_goal_state(_session_goal_raw(metadata)) if metadata else None
    if isinstance(goal, dict) and goal.get("status") == "active":
        objective = str(goal.get("objective") or "").strip()
        if len(objective) > _MAX_OBJECTIVE_WS:
            objective = objective[:_MAX_OBJECTIVE_WS].rstrip() + "…"
        summary = str(goal.get("ui_summary") or "").strip()[:120]
        blob: dict[str, Any] = {"active": True}
        if summary:
            blob["ui_summary"] = summary
        if objective:
            blob["objective"] = objective
        return blob
    return {"active": False}


def runner_wall_llm_timeout_s(
    sessions: SessionManager,
    session_key: str | None,
    *,
    metadata: Mapping[str, Any] | None = None,
    message_metadata: Mapping[str, Any] | None = None,
) -> float | None:
    """Wall-clock cap for :class:`~nanobot.agent.runner.AgentRunner` when streaming an LLM.

    Returns ``0.0`` to disable ``asyncio.wait_for`` around the request when this is a
    sustained-goal turn; ``None`` means use ``NANOBOT_LLM_TIMEOUT_S``. Pass in-memory
    ``metadata`` when the caller already holds :attr:`~nanobot.session.manager.Session.metadata`
    for this turn.
    """
    meta: Mapping[str, Any] | None = metadata
    if meta is None and session_key:
        meta = sessions.get_or_create(session_key).metadata
    return 0.0 if sustained_goal_turn(meta, message_metadata=message_metadata) else None


def webui_interactive_llm_timeout_s(
    *,
    channel: str | None,
    message_metadata: Mapping[str, Any] | None,
) -> float | None:
    """Return the finite timeout for an ordinary WebUI turn.

    A WebUI conversation has one serial turn queue.  Unlike a sustained goal, an
    interactive chat turn must eventually produce a terminal event so the browser
    can release its running state and accept the next message.  ``0`` is an
    explicit operator opt-out; malformed values fall back to the safe default.
    """
    if (
        channel != "websocket"
        or not message_metadata
        or message_metadata.get("webui") is not True
        or is_cron_turn(message_metadata)
    ):
        return None
    raw = os.environ.get(
        _WEBUI_INTERACTIVE_LLM_TIMEOUT_ENV,
        str(DEFAULT_WEBUI_INTERACTIVE_LLM_TIMEOUT_S),
    ).strip()
    try:
        timeout_s = float(raw)
    except ValueError:
        return DEFAULT_WEBUI_INTERACTIVE_LLM_TIMEOUT_S
    if not math.isfinite(timeout_s):
        return DEFAULT_WEBUI_INTERACTIVE_LLM_TIMEOUT_S
    return timeout_s if timeout_s > 0 else None
