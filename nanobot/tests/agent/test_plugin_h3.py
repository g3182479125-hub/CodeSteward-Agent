from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from nanobot.agent.plugins import (
    ManagedPluginContext,
    PluginManager,
    PluginManifest,
    PluginRecord,
    PluginSource,
)
from nanobot.agent.plugins.store import SqlitePluginStateStore
from nanobot.agent.tools.registry import ToolRegistry


def _record(tmp_path: Path, plugin_id: str, version: str, source: str) -> tuple[PluginRecord, Path]:
    root = tmp_path / f"{plugin_id}-{version}"
    root.mkdir()
    entry = root / "tool.py"
    entry.write_text(source.strip() + "\n", encoding="utf-8", newline="\n")
    manifest = PluginManifest.from_mapping(
        {
            "plugin_id": plugin_id,
            "version": version,
            "content_hash": hashlib.sha256(entry.read_bytes()).hexdigest(),
            "kind": "tool",
            "api_version": "nanobot.tool/v1",
            "scope": "session",
            "entry": "tool.py",
            "requires": [],
            "provides": ["tool:demo_versioned"],
            "requested_capabilities": ["read_only"],
            "execution_mode": "in_process",
        }
    )
    return PluginRecord(manifest, PluginSource.DEVELOPER, session_id="cli:direct"), root


_TOOL_PREFIX = """
import asyncio
from nanobot.agent.tools.base import Tool, tool_parameters

@tool_parameters({"type": "object", "properties": {}, "additionalProperties": False})
class VersionTool(Tool):
    @property
    def name(self):
        return "demo_versioned"

    @property
    def description(self):
        return "H3 fixture"

    @property
    def read_only(self):
        return True
"""


@pytest.mark.asyncio
async def test_preparing_context_does_not_open_resources_before_activation() -> None:
    listeners: list[tuple[str, object]] = []
    started = asyncio.Event()

    def subscribe(event_name: str, callback: object):
        listeners.append((event_name, callback))
        return lambda: listeners.remove((event_name, callback))

    async def ticker() -> None:
        started.set()
        await asyncio.sleep(3600)

    context = ManagedPluginContext(subscribe_impl=subscribe)
    context.subscribe("h3", lambda *_args: None)
    handle = context.spawn_task(ticker())
    await asyncio.sleep(0)
    assert not listeners
    assert not started.is_set()

    context.activate()
    await asyncio.wait_for(started.wait(), timeout=1)
    await context.cleanup()

    assert not listeners
    assert handle.task.done()
    assert context.resource_counts == {
        "registrations": 0,
        "listeners": 0,
        "tasks": 0,
        "services": 0,
    }


@pytest.mark.asyncio
async def test_update_switches_route_and_drains_the_old_generation(tmp_path: Path) -> None:
    v1, root1 = _record(
        tmp_path,
        "devflow-versioned",
        "1.0.0",
        _TOOL_PREFIX
        + """
    started = asyncio.Event()
    release = asyncio.Event()
    async def execute(self, **kwargs):
        type(self).started.set()
        await type(self).release.wait()
        return "v1"
""",
    )
    v2, root2 = _record(
        tmp_path,
        "devflow-versioned",
        "2.0.0",
        _TOOL_PREFIX
        + """
    async def execute(self, **kwargs):
        return "v2"
""",
    )
    manager = PluginManager(ToolRegistry())
    assert manager.register_version(v1, root1).ok
    assert manager.register_version(v2, root2).ok
    manager.authorize("cli:direct")
    view = manager.tools_for_session(ToolRegistry(), "cli:direct")
    assert manager.load("devflow-versioned", "1.0.0", session_id="cli:direct").ok

    running = asyncio.create_task(view.execute("demo_versioned", {}))
    old_tool = view.get("demo_versioned")
    assert old_tool is not None
    await asyncio.wait_for(old_tool.started.wait(), timeout=1)  # type: ignore[attr-defined]

    updated = await manager.update(
        "devflow-versioned",
        "2.0.0",
        session_id="cli:direct",
        expected_generation=1,
    )
    assert updated.ok is True
    assert updated.operation_id is not None
    assert await view.execute("demo_versioned", {}) == "v2"

    old_tool.release.set()  # type: ignore[attr-defined]
    assert await running == "v1"
    status = await manager.wait_operation(updated.operation_id, session_id="cli:direct", timeout=1)
    assert status["state"] == "active"
    assert status["details"]["old_cleanup"] == "complete"


@pytest.mark.asyncio
async def test_resource_cleanup_returns_to_baseline_after_repeated_stop(tmp_path: Path) -> None:
    listeners: list[tuple[str, object]] = []

    def subscribe(event_name: str, callback: object):
        listeners.append((event_name, callback))

        def dispose() -> None:
            listeners.remove((event_name, callback))

        return dispose

    source = _TOOL_PREFIX + """
    @classmethod
    def create(cls, ctx):
        async def ticker():
            while True:
                await asyncio.sleep(3600)
        ctx.subscribe("h3", lambda *_args: None)
        ctx.spawn_task(ticker())
        return cls()

    async def execute(self, **kwargs):
        return "resource-ok"
"""
    manager = PluginManager(ToolRegistry(), subscribe_impl=subscribe)
    manager.authorize("cli:direct")
    for index in range(50):
        record, root = _record(tmp_path, "devflow-resource", f"1.0.{index}", source)
        assert manager.register_version(record, root).ok
        assert manager.load("devflow-resource", f"1.0.{index}", session_id="cli:direct").ok
        route = manager._routes[("session", "cli:direct")]["devflow-resource"]  # type: ignore[index]
        context = route.context
        assert len(listeners) == 1
        stopped = manager.stop("devflow-resource", session_id="cli:direct")
        assert stopped.operation_id is not None
        status = await manager.wait_operation(stopped.operation_id, session_id="cli:direct", timeout=1)
        assert status["state"] == "inactive"
        assert not listeners
        assert context.resource_counts == {
            "registrations": 0,
            "listeners": 0,
            "tasks": 0,
            "services": 0,
        }
        assert not manager._retired_routes


@pytest.mark.asyncio
async def test_update_syntax_failure_preserves_the_stable_route(tmp_path: Path) -> None:
    v1, root1 = _record(
        tmp_path,
        "devflow-recover",
        "1.0.0",
        _TOOL_PREFIX + """
    async def execute(self, **kwargs):
        return "stable"
""",
    )
    v2, root2 = _record(
        tmp_path,
        "devflow-recover",
        "2.0.0",
        "this is not valid python (",
    )
    manager = PluginManager(ToolRegistry())
    assert manager.register_version(v1, root1).ok
    assert manager.register_version(v2, root2).ok
    manager.authorize("cli:direct")
    view = manager.tools_for_session(ToolRegistry(), "cli:direct")
    assert manager.load("devflow-recover", "1.0.0", session_id="cli:direct").ok

    result = await manager.update("devflow-recover", "2.0.0", session_id="cli:direct")
    assert result.ok is False
    assert result.code is not None and result.code.value == "plugin_update_failed"
    assert await view.execute("demo_versioned", {}) == "stable"
    rows = {row["version"]: row for row in manager.list_view("cli:direct")}
    assert rows["1.0.0"]["lifecycle"] == "active"
    assert rows["2.0.0"]["lifecycle"] == "failed"


@pytest.mark.asyncio
async def test_update_activation_failure_rolls_back_stable_route(tmp_path: Path) -> None:
    v1, root1 = _record(
        tmp_path,
        "devflow-activation-recover",
        "1.0.0",
        _TOOL_PREFIX
        + """
    async def execute(self, **kwargs):
        return "stable"
""",
    )
    v2, root2 = _record(
        tmp_path,
        "devflow-activation-recover",
        "2.0.0",
        _TOOL_PREFIX
        + """
    @classmethod
    def create(cls, ctx):
        ctx.provide_service("occupied", object())
        return cls()

    async def execute(self, **kwargs):
        return "should-not-run"
""",
    )
    manager = PluginManager(ToolRegistry(), service_registry={"occupied": object()})
    assert manager.register_version(v1, root1).ok
    assert manager.register_version(v2, root2).ok
    manager.authorize("cli:direct")
    view = manager.tools_for_session(ToolRegistry(), "cli:direct")
    assert manager.load("devflow-activation-recover", "1.0.0", session_id="cli:direct").ok

    result = await manager.update("devflow-activation-recover", "2.0.0", session_id="cli:direct")

    assert result.ok is False
    assert result.code is not None and result.code.value == "plugin_update_failed"
    assert result.details_dict["route_commit"] == "rolled_back"
    assert await view.execute("demo_versioned", {}) == "stable"
    rows = {row["version"]: row for row in manager.list_view("cli:direct")}
    assert rows["1.0.0"]["lifecycle"] == "active"
    assert rows["2.0.0"]["lifecycle"] == "failed"


@pytest.mark.asyncio
async def test_concurrent_updates_commit_only_one_generation(tmp_path: Path) -> None:
    v1, root1 = _record(
        tmp_path,
        "devflow-concurrent",
        "1.0.0",
        _TOOL_PREFIX
        + """
    async def execute(self, **kwargs):
        return "v1"
""",
    )
    v2, root2 = _record(
        tmp_path,
        "devflow-concurrent",
        "2.0.0",
        _TOOL_PREFIX
        + """
    async def execute(self, **kwargs):
        return "v2"
""",
    )
    v3, root3 = _record(
        tmp_path,
        "devflow-concurrent",
        "3.0.0",
        _TOOL_PREFIX
        + """
    async def execute(self, **kwargs):
        return "v3"
""",
    )
    manager = PluginManager(ToolRegistry())
    for record, root in ((v1, root1), (v2, root2), (v3, root3)):
        assert manager.register_version(record, root).ok
    manager.authorize("cli:direct")
    assert manager.load("devflow-concurrent", "1.0.0", session_id="cli:direct").ok

    first, second = await asyncio.gather(
        manager.update("devflow-concurrent", "2.0.0", session_id="cli:direct"),
        manager.update("devflow-concurrent", "3.0.0", session_id="cli:direct"),
    )

    assert first.ok is True
    assert second.ok is False
    assert second.code is not None and second.code.value == "plugin_operation_conflict"
    route = manager._routes[("session", "cli:direct")]["devflow-concurrent"]  # type: ignore[index]
    assert route.registration.record.manifest.version == "2.0.0"
    assert route.generation == 2


@pytest.mark.asyncio
async def test_cleanup_failure_is_visible_and_retryable(tmp_path: Path) -> None:
    record, root = _record(
        tmp_path,
        "devflow-cleanup",
        "1.0.0",
        _TOOL_PREFIX
        + """
    @classmethod
    def create(cls, ctx):
        calls = [0]
        async def dispose():
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("fixture cleanup failure")
        ctx.on_dispose(dispose)
        return cls()

    async def execute(self, **kwargs):
        return "cleanup-ok"
""",
    )
    manager = PluginManager(ToolRegistry())
    assert manager.register_version(record, root).ok
    manager.authorize("cli:direct")
    assert manager.load("devflow-cleanup", "1.0.0", session_id="cli:direct").ok
    stopped = manager.stop("devflow-cleanup", session_id="cli:direct")
    assert stopped.operation_id is not None
    failed = await manager.wait_operation(stopped.operation_id, session_id="cli:direct", timeout=1)
    assert failed["state"] == "cleanup_failed"
    assert failed["code"] == "plugin_cleanup_failed"

    retry = await manager.retry_cleanup(stopped.operation_id, session_id="cli:direct")
    assert retry.ok is True
    recovered = await manager.wait_operation(stopped.operation_id, session_id="cli:direct", timeout=1)
    assert recovered["state"] == "inactive"


@pytest.mark.asyncio
async def test_persisted_active_version_is_restored_without_eager_import(tmp_path: Path) -> None:
    state_path = tmp_path / "plugins.json"
    record, root = _record(
        tmp_path,
        "devflow-persisted",
        "1.0.0",
        _TOOL_PREFIX + """
    async def execute(self, **kwargs):
        return "restored"
""",
    )
    first = PluginManager(ToolRegistry(), state_path=state_path)
    assert first.register_version(record, root).ok
    first.authorize("cli:direct")
    loaded = first.load("devflow-persisted", "1.0.0", session_id="cli:direct")
    assert loaded.ok
    assert loaded.operation_id is not None

    second = PluginManager(ToolRegistry(), state_path=state_path)
    second.authorize("cli:direct")
    rows = second.list_view("cli:direct")
    assert rows[0]["lifecycle"] == "active"
    assert rows[0]["generation"] == int(loaded.details_dict["generation"])
    operation = second.operation_status(loaded.operation_id, session_id="cli:direct")
    assert operation is not None
    assert operation["action"] == "load"
    assert operation["state"] == "active"
    view = second.tools_for_session(ToolRegistry(), "cli:direct")
    assert await view.execute("demo_versioned", {}) == "restored"


@pytest.mark.asyncio
async def test_trusted_lifecycle_example_owns_listener_and_timer(tmp_path: Path) -> None:
    example_root = Path(__file__).parents[3] / "devpilot-agent" / "examples" / "pluggable-tool"
    manifest = PluginManifest.from_mapping(
        json.loads((example_root / "plugin-manifest.lifecycle.json").read_text(encoding="utf-8"))
    )
    record = PluginRecord(manifest, PluginSource.DEVELOPER, session_id="cli:direct")
    listeners: list[tuple[str, object]] = []

    def subscribe(event_name: str, callback: object):
        listeners.append((event_name, callback))
        return lambda: listeners.remove((event_name, callback))

    manager = PluginManager(ToolRegistry(), subscribe_impl=subscribe)
    assert manager.register_version(record, example_root).ok
    manager.authorize("cli:direct")
    assert manager.load("devflow-lifecycle", "0.1.0", session_id="cli:direct").ok
    route = manager._routes[("session", "cli:direct")]["devflow-lifecycle"]  # type: ignore[index]
    assert route.context.resource_counts == {
        "registrations": 0,
        "listeners": 1,
        "tasks": 1,
        "services": 0,
    }
    stopped = manager.stop("devflow-lifecycle", session_id="cli:direct")
    assert stopped.operation_id is not None
    status = await manager.wait_operation(stopped.operation_id, session_id="cli:direct", timeout=1)
    assert status["state"] == "inactive"
    assert not listeners
    assert route.context.resource_counts == {
        "registrations": 0,
        "listeners": 0,
        "tasks": 0,
        "services": 0,
    }


@pytest.mark.asyncio
async def test_update_and_stop_replay_the_supplied_operation_id(tmp_path: Path) -> None:
    v1, root1 = _record(
        tmp_path,
        "devflow-idempotent",
        "1.0.0",
        _TOOL_PREFIX
        + """
    async def execute(self, **kwargs):
        return "v1"
""",
    )
    v2, root2 = _record(
        tmp_path,
        "devflow-idempotent",
        "2.0.0",
        _TOOL_PREFIX
        + """
    async def execute(self, **kwargs):
        return "v2"
""",
    )
    manager = PluginManager(ToolRegistry())
    assert manager.register_version(v1, root1).ok
    assert manager.register_version(v2, root2).ok
    manager.authorize("cli:direct")
    assert manager.load("devflow-idempotent", "1.0.0", session_id="cli:direct").ok

    first_update = await manager.update(
        "devflow-idempotent",
        "2.0.0",
        session_id="cli:direct",
        operation_id="update-op-1",
        expected_generation=1,
    )
    replayed_update = await manager.update(
        "devflow-idempotent",
        "2.0.0",
        session_id="cli:direct",
        operation_id="update-op-1",
        expected_generation=999,
    )
    assert first_update == replayed_update
    assert manager._routes[("session", "cli:direct")]["devflow-idempotent"].generation == 2  # type: ignore[index]

    first_stop = manager.stop(
        "devflow-idempotent",
        session_id="cli:direct",
        operation_id="stop-op-1",
        expected_generation=2,
    )
    replayed_stop = manager.stop(
        "devflow-idempotent",
        session_id="cli:direct",
        operation_id="stop-op-1",
        expected_generation=1,
    )
    assert first_stop.to_dict() == replayed_stop.to_dict()


def test_configured_store_bridges_the_previous_alembic_head(tmp_path: Path) -> None:
    database_path = tmp_path / "devpilot.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("create table alembic_version (version_num varchar(64) not null)")
        connection.execute(
            "insert into alembic_version (version_num) values (?)",
            ("0011_p8_skill_improvement_records",),
        )

    store = SqlitePluginStateStore(database_path, require_schema=True)
    store.save({"version": 1, "plugins": [], "active_routes": [], "retired_routes": [], "operations": []})

    assert store.load() == {
        "version": 1,
        "plugins": [],
        "active_routes": [],
        "retired_routes": [],
        "operations": [],
    }


@pytest.mark.asyncio
async def test_remove_replays_with_the_same_operation_id(tmp_path: Path) -> None:
    record, root = _record(
        tmp_path,
        "devflow-remove-idempotent",
        "1.0.0",
        _TOOL_PREFIX
        + """
    async def execute(self, **kwargs):
        return "unused"
""",
    )
    manager = PluginManager(ToolRegistry())
    assert manager.register_version(record, root).ok
    manager.authorize("cli:direct")

    first = manager.remove(
        "devflow-remove-idempotent",
        "1.0.0",
        session_id="cli:direct",
        operation_id="remove-op-1",
    )
    replay = manager.remove(
        "devflow-remove-idempotent",
        "1.0.0",
        session_id="cli:direct",
        operation_id="remove-op-1",
        expected_generation=99,
    )

    assert first.ok is True
    assert first.operation_id == "remove-op-1"
    assert replay.to_dict() == first.to_dict()


@pytest.mark.asyncio
async def test_initialization_failure_exposes_retryable_staging_cleanup(tmp_path: Path) -> None:
    record, root = _record(
        tmp_path,
        "devflow-init-cleanup",
        "1.0.0",
        _TOOL_PREFIX
        + """
    @classmethod
    def create(cls, ctx):
        calls = [0]
        async def dispose():
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("initialization cleanup failure")
        ctx.on_dispose(dispose)
        raise RuntimeError("initialization failed")

    async def execute(self, **kwargs):
        return "unreachable"
""",
    )
    manager = PluginManager(ToolRegistry())
    assert manager.register_version(record, root).ok
    manager.authorize("cli:direct")

    result = manager.load("devflow-init-cleanup", "1.0.0", session_id="cli:direct")
    assert result.ok is False
    assert result.operation_id is not None
    failed = await manager.wait_operation(result.operation_id, session_id="cli:direct", timeout=1)
    assert failed["code"] == "plugin_cleanup_failed"
    recovered = await manager.retry_cleanup(result.operation_id, session_id="cli:direct")
    assert recovered.ok is False
    assert recovered.code is not None and recovered.code.value == "plugin_load_failed"
    assert recovered.details_dict["staging_cleanup"] == "complete"


@pytest.mark.asyncio
async def test_staging_cleanup_failure_is_a_retryable_independent_result(tmp_path: Path) -> None:
    v1, root1 = _record(
        tmp_path,
        "devflow-staging-cleanup",
        "1.0.0",
        _TOOL_PREFIX
        + """
    async def execute(self, **kwargs):
        return "stable"
""",
    )
    v2, root2 = _record(
        tmp_path,
        "devflow-staging-cleanup",
        "2.0.0",
        _TOOL_PREFIX
        + """
    @classmethod
    def create(cls, ctx):
        async def dispose():
            raise RuntimeError("staging cleanup failure")
        ctx.on_dispose(dispose)
        ctx.provide_service("occupied", object())
        return cls()

    async def execute(self, **kwargs):
        return "should-not-run"
""",
    )
    manager = PluginManager(ToolRegistry(), service_registry={"occupied": object()})
    assert manager.register_version(v1, root1).ok
    assert manager.register_version(v2, root2).ok
    manager.authorize("cli:direct")
    assert manager.load("devflow-staging-cleanup", "1.0.0", session_id="cli:direct").ok

    result = await manager.update("devflow-staging-cleanup", "2.0.0", session_id="cli:direct")
    assert result.ok is False
    assert result.code is not None and result.code.value == "plugin_cleanup_failed"
    assert result.operation_id is not None
    assert result.details_dict["route_commit"] == "rolled_back"
    assert result.details_dict["staging_cleanup"] == "failed"

    retry = await manager.retry_cleanup(result.operation_id, session_id="cli:direct")
    assert retry.ok is False
    assert retry.code is not None and retry.code.value == "plugin_cleanup_failed"


@pytest.mark.asyncio
async def test_persisted_retirement_can_be_reconciled_after_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "plugin-runtime.db"
    record, root = _record(
        tmp_path,
        "devflow-restart-cleanup",
        "1.0.0",
        _TOOL_PREFIX
        + """
    @classmethod
    def create(cls, ctx):
        async def dispose():
            raise RuntimeError("process-local cleanup failure")
        ctx.on_dispose(dispose)
        return cls()

    async def execute(self, **kwargs):
        return "ok"
""",
    )
    first = PluginManager(ToolRegistry(), state_path=state_path)
    assert first.register_version(record, root).ok
    first.authorize("cli:direct")
    assert first.load("devflow-restart-cleanup", "1.0.0", session_id="cli:direct").ok
    stopped = first.stop("devflow-restart-cleanup", session_id="cli:direct")
    assert stopped.operation_id is not None
    failed = await first.wait_operation(stopped.operation_id, session_id="cli:direct", timeout=1)
    assert failed["state"] == "cleanup_failed"

    second = PluginManager(ToolRegistry(), state_path=state_path)
    second.authorize("cli:direct")
    restored = second.operation_status(stopped.operation_id, session_id="cli:direct")
    assert restored is not None and restored["state"] == "cleanup_failed"
    recovered = await second.retry_cleanup(stopped.operation_id, session_id="cli:direct")
    assert recovered.ok is True
    assert recovered.details_dict["old_cleanup"] == "recovered_after_restart"
    assert second.list_view("cli:direct")[0]["lifecycle"] == "inactive"


@pytest.mark.asyncio
async def test_forced_process_exit_preserves_retryable_retirement(tmp_path: Path) -> None:
    state_path = tmp_path / "forced-exit.db"
    record, root = _record(
        tmp_path,
        "devflow-forced-exit",
        "1.0.0",
        _TOOL_PREFIX
        + """
    @classmethod
    def create(cls, ctx):
        async def dispose():
            await asyncio.sleep(30)
        ctx.on_dispose(dispose)
        return cls()

    async def execute(self, **kwargs):
        return "ok"
""",
    )
    worker = f'''
import asyncio
import os
from pathlib import Path
from nanobot.agent.plugins import PluginManager, PluginManifest, PluginRecord, PluginSource
from nanobot.agent.tools.registry import ToolRegistry

async def main():
    manifest = PluginManifest.from_mapping({record.manifest.to_dict()!r})
    record = PluginRecord(manifest, PluginSource.DEVELOPER, session_id="cli:direct")
    manager = PluginManager(ToolRegistry(), state_path=Path({str(state_path)!r}))
    manager.register_version(record, Path({str(root)!r}))
    manager.authorize("cli:direct")
    manager.load("devflow-forced-exit", "1.0.0", session_id="cli:direct")
    manager.stop("devflow-forced-exit", session_id="cli:direct")
    os._exit(0)

asyncio.run(main())
'''
    completed = subprocess.run(
        [sys.executable, "-c", worker],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "DEVPILOT_DATA_DIR": "", "DEVPILOT_DATABASE_URL": ""},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    recovered_manager = PluginManager(ToolRegistry(), state_path=state_path)
    recovered_manager.authorize("cli:direct")
    stop_operation_id = next(
        operation_id
        for operation_id, operation in recovered_manager._operations.items()
        if operation.action == "stop"
    )
    operation = recovered_manager.operation_status(
        stop_operation_id, session_id="cli:direct"
    )
    assert operation is not None and operation["action"] == "stop"
    recovered = await recovered_manager.retry_cleanup(
        operation["operation_id"], session_id="cli:direct"
    )
    assert recovered.ok is True
    assert recovered.details_dict["old_cleanup"] == "recovered_after_restart"
