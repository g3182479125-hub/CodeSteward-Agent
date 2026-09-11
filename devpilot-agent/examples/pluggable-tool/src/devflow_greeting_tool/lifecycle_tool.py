from __future__ import annotations

import asyncio

from nanobot.agent.plugins.context import PluginContext
from nanobot.agent.tools.base import Tool, tool_parameters


@tool_parameters({"type": "object", "properties": {}, "additionalProperties": False})
class LifecycleTool(Tool):
    """Trusted H3 fixture that owns one listener and one gated timer task."""

    @classmethod
    def create(cls, ctx: PluginContext) -> Tool:
        async def timer() -> None:
            while True:
                await asyncio.sleep(60)

        ctx.subscribe("devflow.lifecycle", lambda *_args: None)
        ctx.spawn_task(timer())
        return cls()

    @property
    def name(self) -> str:
        return "demo_lifecycle"

    @property
    def description(self) -> str:
        return "Demo H3 tool with host-owned listener and timer resources."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: object) -> str:
        return "lifecycle-ok"
