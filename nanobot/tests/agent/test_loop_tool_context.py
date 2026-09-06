import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop, TurnKind
from nanobot.agent.tools.context import (
    RequestContext,
    bind_request_context,
    current_request_context,
    replace_request_context_attributes,
    reset_request_context,
)
from nanobot.bus.events import InboundMessage, current_inbound_attributes
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session.turn_continuation import INTERNAL_CONTINUATION_META


class _ContextRecordingTool:
    name = "cron"
    concurrency_safe = False

    def __init__(self) -> None:
        self.contexts: list[dict] = []
        self.runtimes: list[object] = []

    async def execute(self, **_kwargs) -> str:
        ctx = current_request_context()
        assert ctx is not None
        self.runtimes.append(ctx.runtime)
        self.contexts.append({
            "channel": ctx.channel,
            "chat_id": ctx.chat_id,
            "metadata": ctx.metadata,
            "session_key": ctx.session_key,
            "attributes": ctx.attributes,
        })
        return "created"


class _Tools:
    def __init__(self, tool: _ContextRecordingTool) -> None:
        self.tool = tool

    @property
    def tool_names(self) -> list[str]:
        return ["cron"]

    def get(self, name: str):
        return self.tool if name == "cron" else None

    def get_definitions(self) -> list:
        return []

    def prepare_call(self, name: str, arguments: dict):
        return (self.tool, arguments, None) if name == "cron" else (None, arguments, None)


@pytest.mark.asyncio
async def test_loop_binds_request_context_for_tool_execution(tmp_path: Path) -> None:
    provider = MagicMock()
    calls = {"n": 0}

    async def chat_with_retry(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="call_1", name="cron", arguments={"action": "add"})],
            )
        return LLMResponse(content="done", tool_calls=[])

    provider.chat_with_retry = chat_with_retry
    provider.get_default_model.return_value = "test-model"

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    cron = _ContextRecordingTool()
    loop.tools = _Tools(cron)

    metadata = {"slack": {"thread_ts": "111.222", "channel_type": "channel"}}
    runtime = loop.llm_runtime()
    request_context = RequestContext(
        channel="slack",
        chat_id="C123",
        metadata=metadata,
        session_key="slack:C123:111.222",
        attributes={"tenant": "acme", "devpilot_confirmation": {"secret": "hidden"}},
        runtime=runtime,
    )
    await loop._run_agent_loop(
        [],
        runtime=runtime,
        channel="slack",
        chat_id="C123",
        metadata=metadata,
        session_key="slack:C123:111.222",
        request_context=request_context,
    )

    assert cron.contexts[-1] == {
        "channel": "slack",
        "chat_id": "C123",
        "metadata": metadata,
        "session_key": "slack:C123:111.222",
        "attributes": {"tenant": "acme", "devpilot_confirmation": {"secret": "hidden"}},
    }
    assert cron.runtimes[-1] is runtime


@pytest.mark.asyncio
async def test_bus_message_attributes_stay_in_transport_context_without_loop_changes() -> None:
    bus = MessageBus()
    await bus.publish_inbound(
        InboundMessage(
            channel="websocket",
            sender_id="user",
            chat_id="chat",
            content="执行",
            attributes={"devpilot_confirmation": {"secret": "hidden"}},
        )
    )
    await bus.consume_inbound()
    assert current_inbound_attributes() == {
        "devpilot_confirmation": {"secret": "hidden"}
    }


def test_request_context_nested_bind_restores_outer_context() -> None:
    outer = RequestContext(channel="slack", chat_id="outer", session_key="slack:outer")
    inner = RequestContext(channel="email", chat_id="inner", session_key="email:inner")

    outer_token = bind_request_context(outer)
    try:
        assert current_request_context() is outer
        inner_token = bind_request_context(inner)
        try:
            assert current_request_context() is inner
        finally:
            reset_request_context(inner_token)
        assert current_request_context() is outer
    finally:
        reset_request_context(outer_token)

    assert current_request_context() is None


def test_pending_message_replaces_only_the_active_request_attributes() -> None:
    token = bind_request_context(
        RequestContext(
            channel="websocket",
            chat_id="chat",
            attributes={"devpilot_confirmation": {"confirmation_id": "old"}},
        )
    )
    try:
        replace_request_context_attributes(
            {"devpilot_confirmation": {"confirmation_id": "new"}}
        )
        context = current_request_context()
        assert context is not None
        assert context.attributes == {
            "devpilot_confirmation": {"confirmation_id": "new"}
        }
    finally:
        reset_request_context(token)


@pytest.mark.asyncio
async def test_prepare_outbound_forwards_proposal_only_as_internal_metadata() -> None:
    loop = object.__new__(AgentLoop)
    loop._assemble_outbound = lambda *_args, **_kwargs: SimpleNamespace(metadata={})
    proposal = {
        "change_set_id": "cs-1",
        "plan_hash": "a" * 64,
        "patch_hash": "b" * 64,
        "scope_hash": "c" * 64,
        "project_id": "project-1",
    }
    ctx = SimpleNamespace(
        suppress_response=False,
        kind=TurnKind.USER,
        msg=SimpleNamespace(),
        final_content="done",
        stop_reason="stop",
        had_injections=False,
        streamed_content=False,
        turn_latency_ms=None,
        require_session=lambda: SimpleNamespace(
            policy=SimpleNamespace(log_content=True)
        ),
        request_context=RequestContext(
            channel="websocket",
            chat_id="chat",
            attributes={"devpilot_proposal": proposal},
        ),
        ephemeral=False,
    )

    await loop._prepare_outbound(ctx)

    assert ctx.outbound.metadata == {"_devpilot_proposal": proposal}


@pytest.mark.asyncio
async def test_prepare_outbound_uses_turn_scoped_proposal_when_context_was_replaced() -> None:
    from devpilot.nanobot_tools import _record_current_turn_proposal

    loop = object.__new__(AgentLoop)
    loop._assemble_outbound = lambda *_args, **_kwargs: SimpleNamespace(metadata={})
    proposal = {
        "change_set_id": "cs-fallback",
        "plan_hash": "a" * 64,
        "patch_hash": "b" * 64,
        "scope_hash": "c" * 64,
        "project_id": "project-fallback",
    }
    ctx = SimpleNamespace(
        suppress_response=False,
        kind=TurnKind.USER,
        msg=SimpleNamespace(),
        final_content="done",
        stop_reason="stop",
        had_injections=False,
        streamed_content=False,
        turn_latency_ms=None,
        require_session=lambda: SimpleNamespace(policy=SimpleNamespace(log_content=True)),
        request_context=RequestContext(channel="websocket", chat_id="chat", attributes={}),
        ephemeral=False,
    )

    _record_current_turn_proposal(proposal)
    await loop._prepare_outbound(ctx)

    assert ctx.outbound.metadata == {"_devpilot_proposal": proposal}


@pytest.mark.asyncio
async def test_request_context_bindings_are_isolated_between_concurrent_tasks() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def observe(ctx: RequestContext, *, wait_first: bool) -> RequestContext | None:
        token = bind_request_context(ctx)
        try:
            if wait_first:
                entered.set()
                await release.wait()
            else:
                await entered.wait()
                release.set()
            await asyncio.sleep(0)
            return current_request_context()
        finally:
            reset_request_context(token)

    first = RequestContext(channel="feishu", chat_id="first", session_key="feishu:first")
    second = RequestContext(channel="telegram", chat_id="second", session_key="telegram:second")

    observed = await asyncio.gather(
        observe(first, wait_first=True),
        observe(second, wait_first=False),
    )

    assert observed == [first, second]
    assert current_request_context() is None


@pytest.mark.asyncio
async def test_agent_loop_restores_outer_request_context_after_runner_exception(
    tmp_path: Path,
) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    outer = RequestContext(channel="test", chat_id="outer", session_key="test:outer")
    runtime = loop.llm_runtime()

    async def fail_run(spec):
        current = current_request_context()
        assert current is not None
        assert spec.runtime is runtime
        assert current.runtime is runtime
        assert current.channel == "slack"
        assert current.chat_id == "C123"
        assert current.session_key == "slack:C123:111.222"
        assert current.original_user_text == "  unchanged user text  "
        raise RuntimeError("runner failed")

    loop.runner.run = AsyncMock(side_effect=fail_run)
    outer_token = bind_request_context(outer)
    try:
        with pytest.raises(RuntimeError, match="runner failed"):
            await loop._run_agent_loop(
                [],
                runtime=runtime,
                channel="slack",
                chat_id="C123",
                session_key="slack:C123:111.222",
                original_user_text="  unchanged user text  ",
            )
        assert current_request_context() is outer
    finally:
        reset_request_context(outer_token)

    assert current_request_context() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({}, "  original user text  "),
        ({INTERNAL_CONTINUATION_META: True}, None),
    ],
)
async def test_process_message_captures_original_text_before_restore(
    tmp_path: Path,
    metadata: dict,
    expected: str | None,
) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    )
    runtime = loop.llm_runtime()
    seen: list[tuple[str | None, object]] = []

    async def stop_after_capture(ctx) -> str:
        seen.append((ctx.original_user_text, ctx.runtime))
        raise RuntimeError("captured before restore")

    loop._restore_turn = stop_after_capture  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="captured before restore"):
        await loop._process_message(
            InboundMessage(
                channel="slack",
                sender_id="user",
                chat_id="C123",
                content="  original user text  ",
                metadata=metadata,
            ),
            runtime=runtime,
        )

    assert seen == [(expected, runtime)]
