"""Execution helpers for session-bound cron jobs."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from typing import TYPE_CHECKING, Any, Protocol

from nanobot.agent.tools.cron import CronTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.cron.session_delivery import origin_delivery_context
from nanobot.cron.session_turns import CRON_DEFER_UNTIL_IDLE_META, CRON_TRIGGER_META
from nanobot.cron.types import CronJob
from nanobot.cron.webui_metadata import cron_proactive_delivery_metadata
from nanobot.utils.prompt_templates import render_template

if TYPE_CHECKING:
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.utils.llm_runtime import LLMRuntime


class BoundCronAgent(Protocol):
    tools: ToolRegistry

    async def submit_cron_turn(self, msg: InboundMessage) -> OutboundMessage | None:
        ...


class CronRunRecorder(Protocol):
    def write_run_record(self, run_id: str, record: dict[str, Any]) -> None:
        ...


def _cron_prompt_ref(prompt: str) -> dict[str, Any]:
    return {
        "id": "cron.agent_turn.reminder",
        "version": 1,
        "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }


def _bound_session_delivery_context(
    job: CronJob,
    *,
    turn_seed: str,
    source_label: str | None,
) -> tuple[str, str, dict[str, Any]]:
    channel, chat_id, metadata = origin_delivery_context(job)

    if channel == "websocket":
        metadata["webui"] = True
        metadata.update(
            cron_proactive_delivery_metadata(
                "websocket",
                metadata,
                turn_seed=turn_seed,
                source_label=source_label,
            )
        )

    return channel, chat_id, metadata


async def run_bound_cron_job(
    job: CronJob,
    *,
    agent: BoundCronAgent,
    cron: CronRunRecorder,
    message_override: str | None = None,
    trusted_attributes: dict[str, Any] | None = None,
    tools: ToolRegistry | None = None,
    session_key_override: str | None = None,
    sender_id_override: str | None = None,
    runtime: LLMRuntime | None = None,
    max_iterations: int | None = None,
    finalize_on_max_iterations: bool | None = None,
    metadata: dict[str, Any] | None = None,
    ephemeral: bool = False,
) -> str | None:
    """Execute a session-bound cron job as a normal agent session turn."""
    session_key = session_key_override or job.payload.session_key
    sender_id = sender_id_override or "cron"
    if not session_key:
        raise ValueError(f"cron job {job.id} is missing payload.session_key")

    prompt = render_template(
        "agent/cron_reminder.md",
        strip=True,
        message=message_override if message_override is not None else job.payload.message,
    )
    prompt_ref = _cron_prompt_ref(prompt)
    effective_message = message_override if message_override is not None else job.payload.message
    run_id = f"{job.id}:{int(time.time() * 1000)}:{uuid.uuid4().hex[:8]}"
    channel, chat_id, turn_metadata = _bound_session_delivery_context(
        job,
        turn_seed=f"cron:{job.id}",
        source_label=job.name,
    )
    if metadata:
        turn_metadata.update(metadata)
    turn_metadata[CRON_TRIGGER_META] = {
        "job_id": job.id,
        "job_name": job.name,
        "run_id": run_id,
        "prompt_ref": prompt_ref,
        "persist_content": (
            f"Scheduled cron job triggered: {job.name}\n\n{job.payload.message}"
        ),
    }
    turn_metadata[CRON_DEFER_UNTIL_IDLE_META] = True
    run_record_base: dict[str, Any] = {
        "job_id": job.id,
        "job_name": job.name,
        "session_key": session_key,
        "prompt_ref": prompt_ref,
        "prompt_vars": {"message": effective_message},
        "rendered_prompt": prompt,
    }

    cron.write_run_record(
        run_id,
        {
            **run_record_base,
            "status": "queued",
        },
    )

    cron_tool = agent.tools.get("cron")
    cron_token = None
    if isinstance(cron_tool, CronTool):
        cron_token = cron_tool.set_cron_context(True)
    try:
        if tools is None:
            resp = await agent.submit_cron_turn(
                InboundMessage(
                    channel=channel,
                    sender_id=sender_id,
                    chat_id=chat_id,
                    content=prompt,
                    metadata=turn_metadata,
                    attributes=dict(trusted_attributes or {}),
                    session_key_override=session_key,
                )
            )
        else:
            process_direct = getattr(agent, "process_direct", None)
            if process_direct is None:
                raise TypeError("restricted cron execution requires process_direct")
            resp = await process_direct(
                prompt,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                sender_id=sender_id,
                tools=tools,
                persist_user_message=False,
                ephemeral=ephemeral,
                runtime=runtime,
                max_iterations=max_iterations,
                finalize_on_max_iterations=finalize_on_max_iterations,
                metadata=turn_metadata,
                attributes=dict(trusted_attributes or {}),
            )
    except (Exception, asyncio.CancelledError) as exc:
        error_text = str(exc) or exc.__class__.__name__
        cron.write_run_record(
            run_id,
            {
                **run_record_base,
                "status": "error",
                "error": error_text,
            },
        )
        raise
    finally:
        if isinstance(cron_tool, CronTool) and cron_token is not None:
            cron_tool.reset_cron_context(cron_token)

    response = resp.content if resp else ""
    cron.write_run_record(
        run_id,
        {
            **run_record_base,
            "status": "ok",
            "response": response,
        },
    )
    return response
