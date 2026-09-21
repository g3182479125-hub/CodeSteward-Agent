"""Structured progress-event helpers shared by agent runtimes."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

from nanobot.agent.hook import AgentHookContext


def on_progress_accepts_tool_events(cb: Callable[..., Any]) -> bool:
    return _on_progress_accepts(cb, "tool_events")


def on_progress_accepts_file_edit_events(cb: Callable[..., Any]) -> bool:
    return _on_progress_accepts(cb, "file_edit_events")


def _on_progress_accepts(cb: Callable[..., Any], name: str) -> bool:
    try:
        sig = inspect.signature(cb)
    except (TypeError, ValueError):
        return False
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return True
    return name in sig.parameters


async def invoke_on_progress(
    on_progress: Callable[..., Awaitable[None]],
    content: str,
    *,
    tool_hint: bool = False,
    tool_events: list[dict[str, Any]] | None = None,
) -> None:
    if tool_events and on_progress_accepts_tool_events(on_progress):
        await on_progress(content, tool_hint=tool_hint, tool_events=tool_events)
        return
    await on_progress(content, tool_hint=tool_hint)


async def invoke_file_edit_progress(
    on_progress: Callable[..., Awaitable[None]],
    file_edit_events: list[dict[str, Any]],
) -> None:
    if not file_edit_events or not on_progress_accepts_file_edit_events(on_progress):
        return
    await on_progress("", file_edit_events=file_edit_events)


def _tool_event_arguments(tool_call: Any) -> dict[str, Any]:
    arguments = getattr(tool_call, "arguments", {}) or {}
    return cast(dict[str, Any], arguments) if isinstance(arguments, dict) else {}


def _temporary_tool_activity(name: str, phase: str) -> dict[str, str] | None:
    """Map temporary-tool control calls to a safe lifecycle activity summary."""
    if name in {"devpilot_validate_temporary_tool", "devpilot_ensure_temporary_tool"}:
        lifecycle = "validating" if phase == "start" else "validated" if phase == "end" else "failed"
    elif name == "devpilot_activate_temporary_tool":
        lifecycle = "validating" if phase == "start" else "active" if phase == "end" else "failed"
    else:
        return None
    return {
        "kind": "temporary_tool",
        "phase": lifecycle,
        "summary": {
            "validating": "正在校验临时工具",
            "validated": "临时工具校验通过",
            "active": "临时工具已启用",
            "failed": "临时工具处理失败，聊天仍可继续",
        }[lifecycle],
    }


def build_tool_event_start_payload(tool_call: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 1,
        "phase": "start",
        "call_id": str(getattr(tool_call, "id", "") or ""),
        "name": getattr(tool_call, "name", ""),
        "arguments": _tool_event_arguments(tool_call),
        "result": None,
        "error": None,
        "files": [],
        "embeds": [],
    }
    activity = _temporary_tool_activity(str(getattr(tool_call, "name", "")), "start")
    if activity is not None:
        payload["capability_activity"] = activity
    return payload


def tool_event_result_extras(result: Any) -> tuple[list[Any], list[Any]]:
    if not isinstance(result, dict):
        return [], []
    result_data = cast(dict[str, Any], result)
    raw_files = result_data.get("files")
    raw_embeds = result_data.get("embeds")
    files: list[Any] = cast(list[Any], raw_files) if isinstance(raw_files, list) else []
    embeds: list[Any] = cast(list[Any], raw_embeds) if isinstance(raw_embeds, list) else []
    return files, embeds


def build_tool_event_finish_payloads(context: AgentHookContext) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    count = min(len(context.tool_calls), len(context.tool_results), len(context.tool_events))
    for idx in range(count):
        tool_call = context.tool_calls[idx]
        result = context.tool_results[idx]
        event = context.tool_events[idx]
        status = event.get("status")
        phase = "end" if status == "ok" else "error"
        files, embeds = tool_event_result_extras(result)
        payload = {
            "version": 1,
            "phase": phase,
            "call_id": str(getattr(tool_call, "id", "") or ""),
            "name": getattr(tool_call, "name", ""),
            "arguments": _tool_event_arguments(tool_call),
            "result": result if phase == "end" else None,
            "error": None,
            "files": files,
            "embeds": embeds,
        }
        # ToolResult remains string-compatible for model providers, while
        # WebUI and logs can consume the stable execution contract when one
        # is available.  Do not add empty fields to legacy events.
        for key in ("code", "decision", "retryable"):
            value = getattr(result, key, None)
            if value is not None:
                payload[key] = value
        if phase == "error":
            if isinstance(result, str) and result.strip():
                payload["error"] = result.strip()
            else:
                payload["error"] = str(event.get("detail") or "Tool execution failed")
        activity = _temporary_tool_activity(str(getattr(tool_call, "name", "")), phase)
        if activity is not None:
            payload["capability_activity"] = activity
        payloads.append(payload)
    return payloads
