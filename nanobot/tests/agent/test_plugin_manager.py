from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from nanobot.agent.plugins import (
    PluginManifest,
    PluginRecord,
    PluginSource,
)
from nanobot.agent.plugins.manager import PluginManager
from nanobot.agent.task_profiles import DynamicPluginPolicy
from nanobot.agent.tools.registry import ToolRegistry


def _example_record(session_id: str = "cli:direct") -> tuple[PluginRecord, Path]:
    root = Path(__file__).parents[3] / "devpilot-agent" / "examples" / "pluggable-tool"
    manifest = PluginManifest.from_mapping(
        json.loads((root / "plugin-manifest.json").read_text(encoding="utf-8"))
    )
    return PluginRecord(manifest, PluginSource.DEVELOPER, session_id=session_id), root


def _blocking_record(tmp_path: Path, session_id: str = "cli:direct") -> tuple[PluginRecord, Path]:
    source = tmp_path / "blocking.py"
    source.write_text(
        """
import asyncio
from nanobot.agent.tools.base import Tool, tool_parameters

@tool_parameters({
    'type': 'object',
    'properties': {},
    'additionalProperties': False,
})
class BlockingTool(Tool):
    started = asyncio.Event()
    release = asyncio.Event()

    @property
    def name(self):
        return 'demo_blocking'

    @property
    def description(self):
        return 'Test-only read-only blocking tool.'

    @property
    def read_only(self):
        return True

    async def execute(self, **kwargs):
        type(self).started.set()
        await type(self).release.wait()
        return 'completed'
""".strip()
        + "\n",
        encoding="utf-8",
    )
    manifest = PluginManifest.from_mapping(
        {
            "plugin_id": "devflow-blocking",
            "version": "0.1.0",
            "content_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
            "kind": "tool",
            "api_version": "nanobot.tool/v1",
            "scope": "session",
            "entry": "blocking.py",
            "requires": [],
            "provides": ["tool:demo_blocking"],
            "requested_capabilities": ["read_only"],
            "execution_mode": "in_process",
        }
    )
    return PluginRecord(manifest, PluginSource.DEVELOPER, session_id=session_id), tmp_path


@pytest.mark.asyncio
async def test_load_and_stop_change_only_the_dynamic_view() -> None:
    base = ToolRegistry()
    manager = PluginManager(base)
    record, root = _example_record()
    manager.register_version(record, root)
    manager.authorize("cli:direct")
    view = manager.tools_for_session(base, "cli:direct")
    base_identity = id(base)

    loaded = manager.load("devflow-greeting", "0.1.0", session_id="cli:direct")

    assert loaded.ok is True
    assert loaded.operation_id is not None
    assert id(base) == base_identity
    assert "demo_greeting" in view.tool_names
    assert await view.execute("demo_greeting", {"name": "小明"}) == "插件工具已加载：你好，小明！"

    stopped = manager.stop("devflow-greeting", session_id="cli:direct")

    assert stopped.ok is True
    assert "demo_greeting" not in view.tool_names
    retired = await view.execute("demo_greeting", {"name": "小明"})
    assert "TOOL_VERSION_RETIRED" in str(retired)


def test_profile_policy_is_required_for_profiled_dynamic_plugins() -> None:
    base = ToolRegistry()
    manager = PluginManager(base)
    record, root = _example_record()
    manager.register_version(record, root)

    result = manager.load(
        "devflow-greeting",
        "0.1.0",
        session_id="cli:direct",
        policy=DynamicPluginPolicy(),
    )

    assert result.ok is False
    assert result.code is not None
    assert result.code.value == "plugin_not_authorized"


def test_disabled_profile_hides_an_authorized_dynamic_route() -> None:
    base = ToolRegistry()
    manager = PluginManager(base)
    record, root = _example_record()
    manager.register_version(record, root)
    manager.authorize("cli:direct")
    assert manager.load("devflow-greeting", "0.1.0", session_id="cli:direct").ok

    ordinary_view = manager.tools_for_session(base, "cli:direct")
    disabled_view = manager.tools_for_session(
        base,
        "cli:direct",
        policy=DynamicPluginPolicy(),
    )

    assert "demo_greeting" in ordinary_view.tool_names
    assert disabled_view is base
    assert "demo_greeting" not in disabled_view.tool_names


def test_session_routes_do_not_leak_to_another_session() -> None:
    base = ToolRegistry()
    manager = PluginManager(base)
    first, root = _example_record("cli:first")
    second, _ = _example_record("cli:second")
    manager.register_version(first, root)
    manager.register_version(second, root)
    manager.authorize("cli:first")
    manager.authorize("cli:second")
    assert manager.load("devflow-greeting", "0.1.0", session_id="cli:first").ok

    first_view = manager.tools_for_session(base, "cli:first")
    second_view = manager.tools_for_session(base, "cli:second")

    assert "demo_greeting" in first_view
    assert "demo_greeting" not in second_view


def test_live_view_observes_an_inline_authorization_without_rebuilding_loop() -> None:
    base = ToolRegistry()
    manager = PluginManager(base)
    record, root = _example_record()
    manager.register_version(record, root)
    view = manager.live_tools_for_session(base, "cli:direct")

    assert "demo_greeting" not in view.tool_names
    manager.authorize("cli:direct")
    assert manager.load("devflow-greeting", "0.1.0", session_id="cli:direct").ok
    assert "demo_greeting" in view.tool_names


def test_cross_session_dynamic_view_is_unwrapped_before_routing() -> None:
    base = ToolRegistry()
    manager = PluginManager(base)
    record, root = _example_record("cli:first")
    manager.register_version(record, root)
    manager.authorize("cli:first")
    assert manager.load("devflow-greeting", "0.1.0", session_id="cli:first").ok
    first_view = manager.tools_for_session(base, "cli:first")

    second_view = manager.tools_for_session(first_view, "cli:second")

    assert second_view is base
    assert "demo_greeting" not in second_view.tool_names


def test_load_rechecks_the_registered_artifact_hash(tmp_path: Path) -> None:
    base = ToolRegistry()
    manager = PluginManager(base)
    record, root = _blocking_record(tmp_path)
    assert manager.register_version(record, root).ok
    source = root / "blocking.py"
    source.write_text(source.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    manager.authorize("cli:direct")

    result = manager.load("devflow-blocking", "0.1.0", session_id="cli:direct")

    assert result.ok is False
    assert result.code is not None
    assert result.code.value == "plugin_load_failed"


@pytest.mark.asyncio
async def test_finished_old_generation_does_not_hide_a_new_generation(tmp_path: Path) -> None:
    base = ToolRegistry()
    manager = PluginManager(base)
    record, root = _blocking_record(tmp_path)
    manager.register_version(record, root)
    manager.authorize("cli:direct")
    view = manager.tools_for_session(base, "cli:direct")
    assert manager.load("devflow-blocking", "0.1.0", session_id="cli:direct").ok

    running = asyncio.create_task(view.execute("demo_blocking", {}))
    tool = view.get("demo_blocking")
    assert tool is not None
    await asyncio.wait_for(tool.started.wait(), timeout=1)  # type: ignore[attr-defined]
    stopped = manager.stop("devflow-blocking", session_id="cli:direct")
    assert stopped.operation_id is not None
    assert manager.load("devflow-blocking", "0.1.0", session_id="cli:direct").ok

    tool.release.set()  # type: ignore[attr-defined]
    assert await running == "completed"
    status = await manager.wait_operation(
        stopped.operation_id,
        session_id="cli:direct",
        timeout=1,
    )

    assert status["state"] == "inactive"
    row = manager.list_view("cli:direct")[0]
    assert row["lifecycle"] == "active"


def test_operation_status_is_scoped_to_the_requesting_session() -> None:
    base = ToolRegistry()
    manager = PluginManager(base)
    record, root = _example_record("cli:first")
    manager.register_version(record, root)
    manager.authorize("cli:first")
    result = manager.load("devflow-greeting", "0.1.0", session_id="cli:first")
    assert result.operation_id is not None

    assert manager.operation_status(result.operation_id, session_id="cli:second") is None
    assert manager.operation_status(result.operation_id, session_id="cli:first") is not None


@pytest.mark.asyncio
async def test_stop_drains_existing_lease_and_retires_next_call(tmp_path: Path) -> None:
    base = ToolRegistry()
    manager = PluginManager(base)
    record, root = _blocking_record(tmp_path)
    manager.register_version(record, root)
    manager.authorize("cli:direct")
    view = manager.tools_for_session(base, "cli:direct")
    manager.load("devflow-blocking", "0.1.0", session_id="cli:direct")

    running = asyncio.create_task(view.execute("demo_blocking", {}))
    tool = view.get("demo_blocking")
    assert tool is not None
    await asyncio.wait_for(tool.started.wait(), timeout=1)  # type: ignore[attr-defined]

    stopped = manager.stop("devflow-blocking", session_id="cli:direct")
    assert stopped.ok is True
    assert stopped.operation_id is not None
    assert stopped.details_dict["state"] == "draining"

    tool.release.set()  # type: ignore[attr-defined]
    assert await running == "completed"
    status = await manager.wait_operation(
        stopped.operation_id,
        session_id="cli:direct",
        timeout=1,
    )
    assert status["state"] == "inactive"
    assert "TOOL_VERSION_RETIRED" in str(await view.execute("demo_blocking", {}))
