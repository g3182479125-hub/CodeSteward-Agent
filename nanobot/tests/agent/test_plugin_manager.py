from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from nanobot.agent.plugins import (
    PluginErrorCode,
    PluginManifest,
    PluginOperationResult,
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


def _temporary_record(tmp_path: Path, session_id: str = "websocket:temporary") -> tuple[PluginRecord, Path]:
    source = tmp_path / "temporary.py"
    source.write_text(
        """
from nanobot.agent.tools.base import Tool, tool_parameters

@tool_parameters({"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]})
class TemporaryTool(Tool):
    @property
    def name(self):
        return "temporary_lookup"

    @property
    def description(self):
        return "Temporary test tool."

    async def execute(self, value):
        return f"temporary:{value}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    manifest = PluginManifest.from_mapping(
        {
            "plugin_id": "temporary-lookup",
            "version": "1.0.0",
            "content_hash": hashlib.sha256(source.read_bytes()).hexdigest(),
            "kind": "tool",
            "api_version": "devpilot.temporary/v1",
            "scope": "session",
            "entry": "temporary.py",
            "requires": [],
            "provides": ["tool:temporary_lookup"],
            "requested_capabilities": [],
            "execution_mode": "in_process",
        }
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text('{"fixture":"manifest"}', encoding="utf-8")
    (tmp_path / "validation.json").write_text(
        json.dumps(
            {
                "validation_version": "temporary-tool-validation/v1",
                "checker_version": "temporary-tool-contract-check/v1",
                "status": "validated",
                "source_hash": manifest.content_hash,
                "manifest_hash": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "tool_id": manifest.plugin_id,
                "version": manifest.version,
                "session_id": session_id,
                "run_id": "run-1",
                "project_id": "project-1",
                "execution_location": "local",
                "execution_run_id": (
                    f"run-1:temporary-tool-validation:{manifest.content_hash[:12]}:fixture"
                ),
                "receipt": {
                    "backend": "local",
                    "sandbox_id": "",
                    "exit_code": 0,
                    "execution_location": "local",
                    "command_profile": "temporary-tool-contract",
                    "network_policy": "host-default-unrestricted",
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "lifecycle.json").write_text(
        json.dumps({"state": "active", "history": ["active"]}),
        encoding="utf-8",
    )
    return PluginRecord(
        manifest,
        PluginSource.TEMPORARY,
        session_id=session_id,
        project_id="project-1",
        run_id="run-1",
    ), tmp_path


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


@pytest.mark.asyncio
async def test_temporary_tool_registers_and_hot_loads_without_restart(tmp_path: Path) -> None:
    base = ToolRegistry()
    manager = PluginManager(base)
    record, root = _temporary_record(tmp_path)
    registered = manager.register_temporary_version(record, root)

    assert registered.ok is True
    manager.authorize("websocket:temporary")
    loaded = manager.activate_temporary(
        "temporary-lookup",
        "1.0.0",
        session_id="websocket:temporary",
        run_id="run-1",
        project_id="project-1",
    )

    assert loaded.ok is True
    view = manager.live_tools_for_session(base, "websocket:temporary", project_id="project-1")
    assert "temporary_lookup" in view.tool_names
    assert await view.execute("temporary_lookup", {"value": "ok"}) == "temporary:ok"
    other_session_view = manager.live_tools_for_session(
        base, "websocket:other", project_id="project-1"
    )
    assert "temporary_lookup" not in other_session_view.tool_names
    other_project_view = manager.live_tools_for_session(
        base, "websocket:temporary", project_id="project-2"
    )
    assert "temporary_lookup" not in other_project_view.tool_names

    cleaned = await manager.cleanup_temporary_run(
        session_id="websocket:temporary",
        run_id="run-1",
    )
    assert cleaned[0]["remove"] == "ok"
    assert "temporary_lookup" not in view.tool_names


@pytest.mark.asyncio
async def test_cleanup_for_one_run_does_not_stop_another_runs_active_route(
    tmp_path: Path,
) -> None:
    base = ToolRegistry()
    manager = PluginManager(base)
    root_a = tmp_path / "run-a"
    root_b = tmp_path / "run-b"
    root_a.mkdir()
    root_b.mkdir()
    run_a, _ = _temporary_record(root_a)
    run_b, _ = _temporary_record(root_b)
    run_b = replace(run_b, run_id="run-2")
    receipt_path = root_b / "validation.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["run_id"] = "run-2"
    receipt["execution_run_id"] = (
        f"run-2:temporary-tool-validation:{run_b.manifest.content_hash[:12]}:fixture"
    )
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert manager.register_temporary_version(run_a, root_a).ok is True
    assert manager.register_temporary_version(run_b, root_b).ok is True
    manager.authorize(run_a.session_id or "")

    assert manager.activate_temporary(
        run_b.manifest.plugin_id,
        run_b.manifest.version,
        session_id=run_b.session_id or "",
        run_id=run_b.run_id or "",
        project_id=run_b.project_id,
    ).ok is True
    view = manager.live_tools_for_session(base, run_b.session_id or "", project_id="project-1")
    assert await view.execute("temporary_lookup", {"value": "still-live"}) == "temporary:still-live"

    await manager.cleanup_temporary_run(
        session_id=run_a.session_id or "",
        run_id=run_a.run_id or "",
    )

    assert await view.execute("temporary_lookup", {"value": "still-live"}) == "temporary:still-live"
    await manager.cleanup_temporary_run(
        session_id=run_b.session_id or "",
        run_id=run_b.run_id or "",
    )
    assert "temporary_lookup" not in view.tool_names


def test_temporary_registration_requires_matching_local_receipt(tmp_path: Path) -> None:
    record, root = _temporary_record(tmp_path)
    (root / "validation.json").unlink()
    result = PluginManager(ToolRegistry()).register_temporary_version(record, root)
    assert result.ok is False
    assert result.code is not None
    assert result.code.value == "plugin_load_failed"


def test_temporary_registration_accepts_only_fully_bound_local_contract_receipt(
    tmp_path: Path,
) -> None:
    record, root = _temporary_record(tmp_path)
    manifest_path = root / "manifest.json"
    manifest_path.write_text('{"fixture":"manifest"}', encoding="utf-8")
    validation_path = root / "validation.json"
    validation_path.write_text(
        json.dumps(
            {
                "validation_version": "temporary-tool-validation/v1",
                "checker_version": "temporary-tool-contract-check/v1",
                "status": "validated",
                "tool_id": record.manifest.plugin_id,
                "version": record.manifest.version,
                "source_hash": record.manifest.content_hash,
                "manifest_hash": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                "session_id": record.session_id,
                "run_id": record.run_id,
                "project_id": record.project_id,
                "execution_run_id": (
                    f"run-1:temporary-tool-validation:{record.manifest.content_hash[:12]}:abc"
                ),
                "execution_location": "local",
                "receipt": {
                    "backend": "local",
                    "sandbox_id": "",
                    "exit_code": 0,
                    "execution_location": "local",
                    "command_profile": "temporary-tool-contract",
                    "network_policy": "host-default-unrestricted",
                },
            }
        ),
        encoding="utf-8",
    )

    assert PluginManager(ToolRegistry()).register_temporary_version(record, root).ok is True

    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["run_id"] = "another-run"
    validation_path.write_text(json.dumps(validation), encoding="utf-8")
    rejected = PluginManager(ToolRegistry()).register_temporary_version(record, root)
    assert rejected.ok is False
    assert rejected.code is not None
    assert rejected.code.value == "plugin_load_failed"

    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["run_id"] = record.run_id
    validation["execution_location"] = "fc"
    validation["receipt"]["backend"] = "fc"
    validation["receipt"]["sandbox_id"] = "legacy-fc-sandbox"
    validation["receipt"]["execution_location"] = "fc"
    validation_path.write_text(json.dumps(validation), encoding="utf-8")
    rejected_fc_receipt = PluginManager(ToolRegistry()).register_temporary_version(record, root)
    assert rejected_fc_receipt.ok is False
    assert rejected_fc_receipt.code is not None
    assert rejected_fc_receipt.code.value == "plugin_load_failed"

    validation["execution_location"] = "local"
    validation["receipt"].update(
        backend="local", sandbox_id="", execution_location="local"
    )
    validation_path.write_text(json.dumps(validation), encoding="utf-8")
    manifest_path.write_text('{"fixture":"tampered"}', encoding="utf-8")
    rejected_tampered_manifest = PluginManager(ToolRegistry()).register_temporary_version(
        record, root
    )
    assert rejected_tampered_manifest.ok is False
    assert rejected_tampered_manifest.code is not None
    assert rejected_tampered_manifest.code.value == "plugin_load_failed"


@pytest.mark.asyncio
async def test_temporary_cleanup_pending_retries_without_persistence_prompt(
    tmp_path: Path,
) -> None:
    base = ToolRegistry()
    manager = PluginManager(base)
    record, root = _temporary_record(tmp_path)
    assert manager.register_temporary_version(record, root).ok is True
    manager.authorize(record.session_id or "")
    assert manager.activate_temporary(
        record.manifest.plugin_id,
        record.manifest.version,
        session_id=record.session_id or "",
        run_id=record.run_id or "",
        project_id=record.project_id,
    ).ok is True

    original_remove = manager.remove
    attempts = 0

    def fail_once(*args: object, **kwargs: object) -> PluginOperationResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return PluginOperationResult.failure(
                PluginErrorCode.PLUGIN_REMOVE_REJECTED,
                "test cleanup failure",
                plugin_id=record.manifest.plugin_id,
                version=record.manifest.version,
            )
        return original_remove(*args, **kwargs)  # type: ignore[arg-type]

    manager.remove = fail_once  # type: ignore[method-assign]
    first = await manager.cleanup_temporary_run(
        session_id=record.session_id or "",
        run_id=record.run_id or "",
    )
    assert first[0]["artifact"] == "cleanup_pending"
    lifecycle = json.loads((root / "lifecycle.json").read_text(encoding="utf-8"))
    assert lifecycle["state"] == "cleanup_pending"
    assert lifecycle["history"][-2:] == ["retiring", "cleanup_pending"]

    second = await manager.cleanup_temporary_run(
        session_id=record.session_id or "",
        run_id=record.run_id or "",
    )
    assert second[0]["artifact"] == "pending_persistence"
    assert attempts == 2


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
    newer = PluginRecord(
        replace(record.manifest, version="0.2.0"),
        PluginSource.DEVELOPER,
        session_id="cli:direct",
    )
    manager.register_version(record, root)
    manager.register_version(newer, root)
    manager.authorize("cli:direct")
    view = manager.tools_for_session(base, "cli:direct")
    assert manager.load("devflow-blocking", "0.1.0", session_id="cli:direct").ok

    running = asyncio.create_task(view.execute("demo_blocking", {}))
    tool = view.get("demo_blocking")
    assert tool is not None
    await asyncio.wait_for(tool.started.wait(), timeout=1)  # type: ignore[attr-defined]
    stopped = manager.stop("devflow-blocking", session_id="cli:direct")
    assert stopped.operation_id is not None
    assert manager.load("devflow-blocking", "0.2.0", session_id="cli:direct").ok

    tool.release.set()  # type: ignore[attr-defined]
    assert await running == "completed"
    status = await manager.wait_operation(
        stopped.operation_id,
        session_id="cli:direct",
        timeout=1,
    )

    assert status["state"] == "inactive"
    rows = {row["version"]: row for row in manager.list_view("cli:direct")}
    assert rows["0.1.0"]["lifecycle"] == "inactive"
    assert rows["0.2.0"]["lifecycle"] == "active"


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
