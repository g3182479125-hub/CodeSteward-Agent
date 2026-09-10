from __future__ import annotations

from nanobot.agent.tools.base import Tool, tool_parameters


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "left": {"type": "integer", "description": "The first number."},
            "right": {"type": "integer", "description": "The second number."},
        },
        "required": ["left", "right"],
        "additionalProperties": False,
    }
)
class AdditionTool(Tool):
    """A second harmless tool implementation for the H1 manifest fixture."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "demo_add"

    @property
    def description(self) -> str:
        return "Demo plugin: add two integers. Read-only and local to the fixture."

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: int) -> int:
        return kwargs["left"] + kwargs["right"]
