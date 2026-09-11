from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.plugins import PluginManifest, PluginRecord, PluginSource
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings


def _make_loop(tmp_path: Path):
    from nanobot.agent.loop import AgentLoop

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")


def _example_record(session_id: str) -> tuple[PluginRecord, Path]:
    root = Path(__file__).parents[3] / "devpilot-agent" / "examples" / "pluggable-tool"
    manifest = PluginManifest.from_mapping(
        json.loads((root / "plugin-manifest.math.json").read_text(encoding="utf-8"))
    )
    return PluginRecord(manifest, PluginSource.DEVELOPER, session_id=session_id), root


def _versioned_records(tmp_path: Path) -> tuple[tuple[PluginRecord, Path], tuple[PluginRecord, Path]]:
    source = '''
from nanobot.agent.tools.base import Tool, tool_parameters

@tool_parameters({"type": "object", "properties": {"left": {"type": "integer"}, "right": {"type": "integer"}}, "required": ["left", "right"], "additionalProperties": False})
class AdditionTool(Tool):
    @property
    def name(self):
        return "demo_add"

    @property
    def description(self):
        return "Versioned command fixture"

    @property
    def read_only(self):
        return True

    async def execute(self, **kwargs):
        return kwargs["left"] + kwargs["right"] + OFFSET
'''
    records: list[tuple[PluginRecord, Path]] = []
    for version, offset in (("0.1.0", 0), ("0.2.0", 10)):
        root = tmp_path / version
        root.mkdir()
        entry = root / "tool.py"
        entry.write_text(f"OFFSET = {offset}\n" + source.strip() + "\n", encoding="utf-8", newline="\n")
        manifest = PluginManifest.from_mapping(
            {
                "plugin_id": "devflow-math",
                "version": version,
                "content_hash": hashlib.sha256(entry.read_bytes()).hexdigest(),
                "kind": "tool",
                "api_version": "nanobot.tool/v1",
                "scope": "session",
                "entry": "tool.py",
                "requires": [],
                "provides": ["tool:demo_add"],
                "requested_capabilities": ["read_only"],
                "execution_mode": "in_process",
            }
        )
        records.append((PluginRecord(manifest, PluginSource.DEVELOPER, session_id="cli:direct"), root))
    return records[0], records[1]


@pytest.mark.asyncio
async def test_native_plugin_commands_control_the_live_view(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    record, root = _example_record("cli:direct")
    assert loop.plugin_manager.register_version(record, root).ok
    pid_before = os.getpid()
    session_key = "cli:direct"
    base_identity = id(loop.tools)

    loaded = await loop.process_direct(
        "/plugins load devflow-math@0.1.0",
        session_key=session_key,
    )

    assert loaded is not None
    assert "plugin loaded" in loaded.content
    assert "operation_id:" in loaded.content
    assert id(loop.tools) == base_identity

    view = loop.plugin_manager.tools_for_session(
        loop.tools,
        session_key,
        project_id=loop.plugin_manager.project_id_for_path(tmp_path),
    )
    assert "demo_add" in view.tool_names
    assert await view.execute("demo_add", {"left": 2, "right": 3}) == 5

    stopped = await loop.process_direct("/plugins stop devflow-math", session_key=session_key)

    assert stopped is not None
    assert "plugin stopped" in stopped.content
    assert "demo_add" not in view.tool_names
    assert os.getpid() == pid_before


@pytest.mark.asyncio
async def test_native_plugin_commands_update_and_remove_versions(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    first, second = _versioned_records(tmp_path)
    assert loop.plugin_manager.register_version(*first).ok
    assert loop.plugin_manager.register_version(*second).ok

    loaded = await loop.process_direct("/plugins load devflow-math@0.1.0", session_key="cli:direct")
    assert loaded is not None and "plugin loaded" in loaded.content

    updated = await loop.process_direct("/plugins update devflow-math@0.2.0", session_key="cli:direct")
    assert updated is not None and "plugin update completed" in updated.content
    view = loop.plugin_manager.tools_for_session(loop.tools, "cli:direct")
    assert await view.execute("demo_add", {"left": 2, "right": 3}) == 15

    removed_old = await loop.process_direct("/plugins remove devflow-math@0.1.0", session_key="cli:direct")
    assert removed_old is not None and "plugin version removed" in removed_old.content

    stopped = await loop.process_direct("/plugins stop devflow-math", session_key="cli:direct")
    assert stopped is not None and "plugin stopped" in stopped.content
    removed_new = await loop.process_direct("/plugins remove devflow-math@0.2.0", session_key="cli:direct")
    assert removed_new is not None and "plugin version removed" in removed_new.content


def test_plugins_command_is_visible_in_help_and_palette() -> None:
    from nanobot.command.builtin import build_help_text, builtin_command_palette

    help_text = build_help_text()
    assert "/plugins" in help_text
    assert "update <plugin-id>@<version>" in help_text
    assert "remove <plugin-id>@<version>" in help_text
    spec = next(item for item in builtin_command_palette() if item["command"] == "/plugins")
    assert spec["accepts_args"] is True
    assert spec["lifecycle"] == "side_channel"


def test_dynamic_view_without_authorization_is_the_original_registry() -> None:
    from nanobot.agent.plugins.manager import PluginManager

    base = ToolRegistry()
    manager = PluginManager(base)

    assert manager.tools_for_session(base, "cli:direct") is base
