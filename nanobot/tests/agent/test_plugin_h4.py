import asyncio
import hashlib
from pathlib import Path

import pytest

from nanobot.agent.plugins import (
    PluginContractError,
    PluginErrorCode,
    PluginManager,
    PluginManifest,
    PluginRecord,
    PluginScope,
    PluginSource,
)
from nanobot.agent.tools.registry import ToolRegistry


def _record(
    tmp_path: Path,
    plugin_id: str,
    *,
    provides: list[str],
    requires: list[str],
    result: str,
) -> tuple[PluginRecord, Path]:
    root = tmp_path / plugin_id
    root.mkdir()
    entry = root / "tool.py"
    entry.write_text(
        f'''
from nanobot.agent.tools.base import Tool, tool_parameters

@tool_parameters({{"type": "object", "properties": {{}}, "additionalProperties": False}})
class H4Tool(Tool):
    @property
    def name(self):
        return "{plugin_id}-tool"

    @property
    def description(self):
        return "H4 fixture"

    @property
    def read_only(self):
        return True

    async def execute(self, **kwargs):
        return "{result}"
'''.strip()
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = PluginManifest.from_mapping(
        {
            "plugin_id": plugin_id,
            "version": "1.0.0",
            "content_hash": hashlib.sha256(entry.read_bytes()).hexdigest(),
            "kind": "tool",
            "api_version": "nanobot.tool/v1",
            "scope": "session",
            "entry": "tool.py",
            "requires": requires,
            "provides": [f"tool:{plugin_id}-tool", *provides],
            "requested_capabilities": ["read_only"],
            "execution_mode": "in_process",
        }
    )
    return PluginRecord(manifest, PluginSource.DEVELOPER, session_id="cli:direct"), root


def _service_record(
    tmp_path: Path,
    plugin_id: str,
    *,
    version: str = "1.0.0",
    interface: str,
    value: str,
    requires: list[str] | None = None,
    consumer: bool = False,
    blocking: bool = False,
    scope: str = "session",
    session_id: str | None = "cli:direct",
    project_id: str | None = None,
) -> tuple[PluginRecord, Path]:
    root = tmp_path / f"{plugin_id}-{version}"
    root.mkdir()
    entry = root / "tool.py"
    blocking_members = (
        "    started = asyncio.Event()\n"
        "    release = asyncio.Event()\n"
        if blocking
        else ""
    )
    if consumer:
        create = (
            "    @classmethod\n"
            "    def create(cls, ctx):\n"
            "        tool = cls()\n"
            "        tool.context = ctx\n"
            "        return tool\n"
        )
        execute = (
            ("type(self).started.set()\n        await type(self).release.wait()\n        " if blocking else "")
            + ("return self.context.get_service(%r)" % interface)
        )
    else:
        create = (
            "    @classmethod\n"
            "    def create(cls, ctx):\n"
            "        ctx.provide_service(%r, %r)\n"
            "        return cls()\n"
        ) % (interface, value)
        execute = "return %r" % value
    entry.write_text(
        f'''\nimport asyncio
from nanobot.agent.tools.base import Tool, tool_parameters

@tool_parameters({{"type": "object", "properties": {{}}, "additionalProperties": False}})
class H4ServiceTool(Tool):
{blocking_members}
    @property
    def name(self):
        return "{plugin_id}-tool"

    @property
    def description(self):
        return "H4 service fixture"

    @property
    def read_only(self):
        return True

{create}
    async def execute(self, **kwargs):
        {execute}
'''.strip()
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = PluginManifest.from_mapping(
        {
            "plugin_id": plugin_id,
            "version": version,
            "content_hash": hashlib.sha256(entry.read_bytes()).hexdigest(),
            "kind": "tool",
            "api_version": "nanobot.tool/v1",
            "scope": scope,
            "entry": "tool.py",
            "requires": requires or [],
            "provides": [f"tool:{plugin_id}-tool"] + ([] if consumer else [interface]),
            "requested_capabilities": ["read_only"],
            "execution_mode": "in_process",
        }
    )
    return PluginRecord(
        manifest,
        PluginSource.DEVELOPER,
        session_id=session_id if scope == "session" else None,
        project_id=project_id if scope == "project" else None,
    ), root


def _register(manager: PluginManager, record_and_root: tuple[PluginRecord, Path]) -> None:
    record, root = record_and_root
    assert manager.register_version(record, root).ok


@pytest.mark.asyncio
async def test_consumer_waits_then_activates_when_provider_is_loaded(tmp_path: Path) -> None:
    interface = "service:greeting@v1"
    provider = _record(
        tmp_path,
        "devflow-provider",
        provides=[interface],
        requires=[],
        result="provider",
    )
    consumer = _record(
        tmp_path,
        "devflow-consumer",
        provides=[],
        requires=[interface],
        result="consumer",
    )
    manager = PluginManager(ToolRegistry())
    _register(manager, consumer)
    _register(manager, provider)
    manager.authorize("cli:direct")
    view = manager.tools_for_session(ToolRegistry(), "cli:direct")

    waiting = manager.load("devflow-consumer", "1.0.0", session_id="cli:direct")
    assert waiting.code is PluginErrorCode.PLUGIN_DEPENDENCY_UNAVAILABLE
    assert waiting.details_dict["state"] == "waiting"
    assert "devflow-consumer-tool" not in view.tool_names

    loaded = manager.load("devflow-provider", "1.0.0", session_id="cli:direct")
    assert loaded.ok is True
    assert await view.execute("devflow-consumer-tool", {}) == "consumer"
    rows = {row["plugin_id"]: row for row in manager.list_view("cli:direct")}
    assert rows["devflow-consumer"]["lifecycle"] == "active"


@pytest.mark.asyncio
async def test_stopping_provider_drains_consumer_and_reload_recovers_it(tmp_path: Path) -> None:
    interface = "service:clock@v1"
    provider = _record(tmp_path, "devflow-clock", provides=[interface], requires=[], result="clock")
    consumer = _record(tmp_path, "devflow-calendar", provides=[], requires=[interface], result="calendar")
    manager = PluginManager(ToolRegistry())
    _register(manager, provider)
    _register(manager, consumer)
    manager.authorize("cli:direct")
    view = manager.tools_for_session(ToolRegistry(), "cli:direct")
    assert manager.load("devflow-clock", "1.0.0", session_id="cli:direct").ok
    assert manager.load("devflow-calendar", "1.0.0", session_id="cli:direct").ok

    stopped = manager.stop("devflow-clock", session_id="cli:direct")
    assert stopped.ok is True
    assert "devflow-calendar-tool" not in view.tool_names
    rows = {row["plugin_id"]: row for row in manager.list_view("cli:direct")}
    assert rows["devflow-calendar"]["lifecycle"] == "waiting"

    assert manager.load("devflow-clock", "1.0.0", session_id="cli:direct").ok
    assert await view.execute("devflow-calendar-tool", {}) == "calendar"


@pytest.mark.asyncio
async def test_stopping_provider_suspends_the_affected_subgraph(tmp_path: Path) -> None:
    root = _record(
        tmp_path,
        "devflow-root",
        provides=["service:root@v1"],
        requires=[],
        result="root",
    )
    middle = _record(
        tmp_path,
        "devflow-middle",
        provides=["service:middle@v1"],
        requires=["service:root@v1"],
        result="middle",
    )
    leaf = _record(
        tmp_path,
        "devflow-leaf",
        provides=[],
        requires=["service:middle@v1"],
        result="leaf",
    )
    manager = PluginManager(ToolRegistry())
    for item in (root, middle, leaf):
        _register(manager, item)
    manager.authorize("cli:direct")
    view = manager.tools_for_session(ToolRegistry(), "cli:direct")
    assert manager.load("devflow-root", "1.0.0", session_id="cli:direct").ok
    assert manager.load("devflow-middle", "1.0.0", session_id="cli:direct").ok
    assert manager.load("devflow-leaf", "1.0.0", session_id="cli:direct").ok

    assert manager.stop("devflow-root", session_id="cli:direct").ok
    assert "devflow-middle-tool" not in view.tool_names
    assert "devflow-leaf-tool" not in view.tool_names
    rows = {row["plugin_id"]: row for row in manager.list_view("cli:direct")}
    assert rows["devflow-middle"]["lifecycle"] == "waiting"
    assert rows["devflow-leaf"]["lifecycle"] == "waiting"

    assert manager.load("devflow-root", "1.0.0", session_id="cli:direct").ok
    assert await view.execute("devflow-leaf-tool", {}) == "leaf"


@pytest.mark.asyncio
async def test_provider_update_switches_the_scoped_service_binding(tmp_path: Path) -> None:
    interface = "service:greeting@v1"
    provider_v1 = _service_record(
        tmp_path,
        "devflow-service-provider",
        interface=interface,
        value="v1",
    )
    provider_v2 = _service_record(
        tmp_path,
        "devflow-service-provider",
        version="2.0.0",
        interface=interface,
        value="v2",
    )
    consumer = _service_record(
        tmp_path,
        "devflow-service-consumer",
        interface=interface,
        value="unused",
        requires=[interface],
        consumer=True,
    )
    manager = PluginManager(ToolRegistry())
    for item in (provider_v1, provider_v2, consumer):
        _register(manager, item)
    manager.authorize("cli:direct")
    view = manager.tools_for_session(ToolRegistry(), "cli:direct")
    assert manager.load("devflow-service-provider", "1.0.0", session_id="cli:direct").ok
    assert manager.load("devflow-service-consumer", "1.0.0", session_id="cli:direct").ok
    assert await view.execute("devflow-service-consumer-tool", {}) == "v1"

    updated = await manager.update(
        "devflow-service-provider",
        "2.0.0",
        session_id="cli:direct",
        expected_generation=1,
    )
    assert updated.ok is True
    assert await view.execute("devflow-service-consumer-tool", {}) == "v2"


@pytest.mark.asyncio
async def test_provider_update_waits_for_the_whole_dependency_lease(tmp_path: Path) -> None:
    interface = "service:greeting@v1"
    provider_v1 = _service_record(
        tmp_path,
        "devflow-blocking-provider",
        interface=interface,
        value="v1",
    )
    provider_v2 = _service_record(
        tmp_path,
        "devflow-blocking-provider",
        version="2.0.0",
        interface=interface,
        value="v2",
    )
    consumer = _service_record(
        tmp_path,
        "devflow-blocking-consumer",
        interface=interface,
        value="unused",
        requires=[interface],
        consumer=True,
        blocking=True,
    )
    late_consumer = _record(
        tmp_path,
        "devflow-late-consumer",
        provides=[],
        requires=[interface],
        result="late",
    )
    manager = PluginManager(ToolRegistry(), drain_timeout_s=1)
    for item in (provider_v1, provider_v2, consumer, late_consumer):
        _register(manager, item)
    manager.authorize("cli:direct")
    view = manager.tools_for_session(ToolRegistry(), "cli:direct")
    assert manager.load("devflow-blocking-provider", "1.0.0", session_id="cli:direct").ok
    assert manager.load("devflow-blocking-consumer", "1.0.0", session_id="cli:direct").ok

    running = asyncio.create_task(view.execute("devflow-blocking-consumer-tool", {}))
    tool = view.get("devflow-blocking-consumer-tool")
    assert tool is not None
    await asyncio.wait_for(tool.started.wait(), timeout=1)  # type: ignore[attr-defined]

    updating = asyncio.create_task(
        manager.update(
            "devflow-blocking-provider",
            "2.0.0",
            session_id="cli:direct",
            expected_generation=1,
        )
    )
    await asyncio.sleep(0.05)
    assert not updating.done()
    blocked = await view.execute("devflow-blocking-consumer-tool", {})
    assert "PLUGIN_DEPENDENCY_SWITCHING" in blocked
    blocked_load = manager.load("devflow-late-consumer", "1.0.0", session_id="cli:direct")
    assert blocked_load.code is PluginErrorCode.PLUGIN_DEPENDENCY_SWITCHING
    tool.release.set()  # type: ignore[attr-defined]
    assert await running == "v1"
    assert (await updating).ok is True
    assert await view.execute("devflow-blocking-consumer-tool", {}) == "v2"
    assert manager.load("devflow-late-consumer", "1.0.0", session_id="cli:direct").ok


@pytest.mark.asyncio
async def test_provider_update_rejects_breaking_an_active_consumer(tmp_path: Path) -> None:
    old_interface = "service:greeting@v1"
    provider_v1 = _service_record(
        tmp_path,
        "devflow-breaking-provider",
        interface=old_interface,
        value="v1",
    )
    provider_v2 = _service_record(
        tmp_path,
        "devflow-breaking-provider",
        version="2.0.0",
        interface="service:other@v1",
        value="v2",
    )
    consumer = _service_record(
        tmp_path,
        "devflow-breaking-consumer",
        interface=old_interface,
        value="unused",
        requires=[old_interface],
        consumer=True,
    )
    manager = PluginManager(ToolRegistry())
    for item in (provider_v1, provider_v2, consumer):
        _register(manager, item)
    manager.authorize("cli:direct")
    view = manager.tools_for_session(ToolRegistry(), "cli:direct")
    assert manager.load("devflow-breaking-provider", "1.0.0", session_id="cli:direct").ok
    assert manager.load("devflow-breaking-consumer", "1.0.0", session_id="cli:direct").ok

    result = await manager.update(
        "devflow-breaking-provider",
        "2.0.0",
        session_id="cli:direct",
        expected_generation=1,
    )
    assert result.code is PluginErrorCode.PLUGIN_DEPENDENCY_UNAVAILABLE
    assert await view.execute("devflow-breaking-consumer-tool", {}) == "v1"


def test_service_bindings_are_isolated_by_project_scope(tmp_path: Path) -> None:
    interface = "service:search@v1"
    project_a = _service_record(
        tmp_path,
        "devflow-project-service-a",
        interface=interface,
        value="a",
        scope="project",
        session_id=None,
        project_id="project-a",
    )
    project_b = _service_record(
        tmp_path,
        "devflow-project-service-b",
        interface=interface,
        value="b",
        scope="project",
        session_id=None,
        project_id="project-b",
    )
    manager = PluginManager(ToolRegistry())
    _register(manager, project_a)
    _register(manager, project_b)
    manager.authorize("cli:direct", "project-a")
    manager.authorize("cli:direct", "project-b")
    assert manager.load(
        "devflow-project-service-a",
        "1.0.0",
        session_id="cli:direct",
        project_id="project-a",
    ).ok
    assert manager.load(
        "devflow-project-service-b",
        "1.0.0",
        session_id="cli:direct",
        project_id="project-b",
    ).ok
    assert manager._service_registries[(PluginScope.PROJECT, "project-a")][interface] == "a"
    assert manager._service_registries[(PluginScope.PROJECT, "project-b")][interface] == "b"


def test_same_interface_has_one_provider_per_scope(tmp_path: Path) -> None:
    interface = "service:search@v1"
    first = _record(tmp_path, "devflow-search-a", provides=[interface], requires=[], result="a")
    second = _record(tmp_path, "devflow-search-b", provides=[interface], requires=[], result="b")
    manager = PluginManager(ToolRegistry())
    _register(manager, first)
    _register(manager, second)
    manager.authorize("cli:direct")
    assert manager.load("devflow-search-a", "1.0.0", session_id="cli:direct").ok
    conflict = manager.load("devflow-search-b", "1.0.0", session_id="cli:direct")
    assert conflict.code is PluginErrorCode.PLUGIN_DEPENDENCY_CONFLICT


def test_interface_ids_must_be_versioned(tmp_path: Path) -> None:
    root = tmp_path / "invalid-interface"
    root.mkdir()
    entry = root / "tool.py"
    entry.write_text("# fixture\n", encoding="utf-8")
    with pytest.raises(PluginContractError, match="versioned interface IDs"):
        PluginManifest.from_mapping(
            {
                "plugin_id": "devflow-invalid-interface",
                "version": "1.0.0",
                "content_hash": hashlib.sha256(entry.read_bytes()).hexdigest(),
                "kind": "tool",
                "api_version": "nanobot.tool/v1",
                "scope": "session",
                "entry": "tool.py",
                "requires": ["service:search"],
                "provides": ["tool:devflow-invalid-interface"],
                "requested_capabilities": ["read_only"],
                "execution_mode": "in_process",
            }
        )


def test_incompatible_interface_version_stays_waiting(tmp_path: Path) -> None:
    provider = _record(
        tmp_path,
        "devflow-search-v2",
        provides=["service:search@v2"],
        requires=[],
        result="v2",
    )
    consumer = _record(
        tmp_path,
        "devflow-search-consumer",
        provides=[],
        requires=["service:search@v1"],
        result="consumer",
    )
    manager = PluginManager(ToolRegistry())
    _register(manager, provider)
    _register(manager, consumer)
    manager.authorize("cli:direct")
    waiting = manager.load("devflow-search-consumer", "1.0.0", session_id="cli:direct")
    assert waiting.code is PluginErrorCode.PLUGIN_DEPENDENCY_UNAVAILABLE
    assert manager.load("devflow-search-v2", "1.0.0", session_id="cli:direct").ok
    assert manager.tools_for_session(ToolRegistry(), "cli:direct").get("devflow-search-consumer-tool") is None


def test_cycle_is_rejected_before_any_plugin_is_activated(tmp_path: Path) -> None:
    a_to_b = _record(
        tmp_path,
        "devflow-cycle-a",
        provides=["service:a@v1"],
        requires=["service:b@v1"],
        result="a",
    )
    b_to_a = _record(
        tmp_path,
        "devflow-cycle-b",
        provides=["service:b@v1"],
        requires=["service:a@v1"],
        result="b",
    )
    manager = PluginManager(ToolRegistry())
    _register(manager, a_to_b)
    _register(manager, b_to_a)
    manager.authorize("cli:direct")

    result = manager.load("devflow-cycle-a", "1.0.0", session_id="cli:direct")
    assert result.code is PluginErrorCode.PLUGIN_DEPENDENCY_CYCLE
    assert not manager._routes


def test_project_scopes_do_not_share_same_interface_provider(tmp_path: Path) -> None:
    interface = "service:search@v1"
    roots: list[tuple[PluginRecord, Path]] = []
    for project_id, result in (("project-a", "a"), ("project-b", "b")):
        root = tmp_path / project_id
        root.mkdir()
        entry = root / "tool.py"
        entry.write_text(
            f'''
from nanobot.agent.tools.base import Tool, tool_parameters
@tool_parameters({{"type": "object", "properties": {{}}, "additionalProperties": False}})
class SearchTool(Tool):
    @property
    def name(self): return "{project_id}-search"
    @property
    def description(self): return "project fixture"
    @property
    def read_only(self): return True
    async def execute(self, **kwargs): return "{result}"
'''.strip()
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        manifest = PluginManifest.from_mapping(
            {
                "plugin_id": f"devflow-{project_id}",
                "version": "1.0.0",
                "content_hash": hashlib.sha256(entry.read_bytes()).hexdigest(),
                "kind": "tool",
                "api_version": "nanobot.tool/v1",
                "scope": "project",
                "entry": "tool.py",
                "requires": [],
                "provides": [f"tool:{project_id}-search", interface],
                "requested_capabilities": ["read_only"],
                "execution_mode": "in_process",
            }
        )
        roots.append((PluginRecord(manifest, PluginSource.DEVELOPER, project_id=project_id), root))

    manager = PluginManager(ToolRegistry())
    for record_and_root in roots:
        _register(manager, record_and_root)
    manager.authorize("cli:direct", "project-a")
    manager.authorize("cli:direct", "project-b")
    assert manager.load("devflow-project-a", "1.0.0", session_id="cli:direct", project_id="project-a").ok
    assert manager.load("devflow-project-b", "1.0.0", session_id="cli:direct", project_id="project-b").ok
