from __future__ import annotations

from nanobot.agent.tools.base import Tool, tool_parameters


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 80,
                "description": "The name to include in the greeting.",
            }
        },
        "required": ["name"],
        "additionalProperties": False,
    }
)
class WelcomeTool(Tool):
    """A harmless read-only tool that proves external entry-point discovery."""

    _scopes = {"core"}

    @property
    def name(self) -> str:
        return "demo_greeting"

    @property
    def description(self) -> str:
        return (
            "Demo plugin: return a greeting for a name. Read-only; it does not access "
            "files, the network, or the DevPilot control plane."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: str) -> str:
        name = kwargs["name"].strip()
        return f"插件工具已加载：你好，{name}！"
