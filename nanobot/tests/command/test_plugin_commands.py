from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop_driver import LoopCheckpoint
from nanobot.agent.loop_handoff import SqliteLoopHandoffStore
from nanobot.agent.plugins import PluginManifest, PluginRecord, PluginSource
from nanobot.agent.task_profiles import DynamicPluginPolicy, TaskProfile, TaskProfileCatalog
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings


@pytest.fixture(autouse=True)
def _isolate_plugin_command_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep command tests independent from a developer's live DevPilot store."""
    monkeypatch.delenv("DEVPILOT_DATABASE_URL", raising=False)
    monkeypatch.delenv("DEVPILOT_DATA_DIR", raising=False)


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


def _versioned_records(
    tmp_path: Path,
) -> tuple[tuple[PluginRecord, Path], tuple[PluginRecord, Path]]:
    source = """
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
"""
    records: list[tuple[PluginRecord, Path]] = []
    for version, offset in (("0.1.0", 0), ("0.2.0", 10)):
        root = tmp_path / version
        root.mkdir()
        entry = root / "tool.py"
        entry.write_text(
            f"OFFSET = {offset}\n" + source.strip() + "\n", encoding="utf-8", newline="\n"
        )
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
        records.append(
            (PluginRecord(manifest, PluginSource.DEVELOPER, session_id="cli:direct"), root)
        )
    return records[0], records[1]


def _enable_loop_profile(loop) -> None:
    loop.task_profiles = TaskProfileCatalog(
        {
            "test-loop": TaskProfile(
                name="test-loop",
                skill_names=("my",),
                allowed_tools=frozenset(loop.tools.tool_names),
                output_contract=("result",),
                dynamic_plugins=DynamicPluginPolicy(
                    enabled=True,
                    allowed_kinds=frozenset({"loop"}),
                    allowed_actions=frozenset({"switch-loop"}),
                    allowed_scopes=frozenset({"session"}),
                ),
            ),
        }
    )
    session = loop.sessions.get_or_create("cli:direct")
    session.metadata["task_profile"] = "test-loop"
    session.metadata["task_profile_version"] = "1"


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

    updated = await loop.process_direct(
        "/plugins update devflow-math@0.2.0", session_key="cli:direct"
    )
    assert updated is not None and "plugin update completed" in updated.content
    view = loop.plugin_manager.tools_for_session(loop.tools, "cli:direct")
    assert await view.execute("demo_add", {"left": 2, "right": 3}) == 15

    removed_old = await loop.process_direct(
        "/plugins remove devflow-math@0.1.0", session_key="cli:direct"
    )
    assert removed_old is not None and "plugin version removed" in removed_old.content

    stopped = await loop.process_direct("/plugins stop devflow-math", session_key="cli:direct")
    assert stopped is not None and "plugin stopped" in stopped.content
    removed_new = await loop.process_direct(
        "/plugins remove devflow-math@0.2.0", session_key="cli:direct"
    )
    assert removed_new is not None and "plugin version removed" in removed_new.content


@pytest.mark.asyncio
async def test_plugins_switch_loop_queues_boundary_handoff(tmp_path: Path, monkeypatch) -> None:
    database_path = tmp_path / "devpilot.db"
    SqliteLoopHandoffStore(database_path).load("missing")
    monkeypatch.setenv("DEVPILOT_ENABLE_LOOP_HANDOFF", "1")
    monkeypatch.setenv("DEVPILOT_DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    loop = _make_loop(tmp_path)
    assert loop.loop_handoff_manager is not None
    _enable_loop_profile(loop)
    checkpoint = LoopCheckpoint("cli:direct", "task", "project", 0, 0, 3)
    loop.loop_handoff_manager.ensure(checkpoint)

    response = await loop.process_direct(
        "/plugins switch-loop plan-then-act", session_key="cli:direct"
    )

    assert response is not None
    assert "pending" in response.content
    record = loop.loop_handoff_manager.store.load("cli:direct")
    assert record is not None and record.status == "switch_pending"

    status = await loop.process_direct(
        f"/plugins status {record.operation_id}", session_key="cli:direct"
    )
    assert status is not None and "Loop switch status: switch_pending" in status.content


@pytest.mark.asyncio
async def test_plugins_switch_loop_accepts_a_hash_pinned_remote_strategy(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    _enable_loop_profile(loop)
    loop.request_remote_loop_driver_switch = MagicMock(return_value="remote-operation")

    response = await loop.process_direct(
        "/plugins switch-loop remote strategy-one@1.2.3 " + "a" * 64,
        session_key="cli:direct",
    )

    assert response is not None and "pending" in response.content
    loop.request_remote_loop_driver_switch.assert_called_once_with(
        "cli:direct", "strategy-one", "1.2.3", "a" * 64
    )


def test_plugins_command_is_visible_in_help_and_palette() -> None:
    from nanobot.command.builtin import build_help_text, builtin_command_palette

    help_text = build_help_text()
    assert "/plugins" in help_text
    assert "update <plugin-id>@<version>" in help_text
    assert "remove <plugin-id>@<version>" in help_text
    spec = next(item for item in builtin_command_palette() if item["command"] == "/plugins")
    assert spec["accepts_args"] is True
    assert spec["lifecycle"] == "side_channel"


@pytest.mark.asyncio
async def test_plugins_selection_uses_a_bound_native_confirmation(tmp_path: Path) -> None:
    loop = _make_loop(tmp_path)
    loop.task_profiles = TaskProfileCatalog(
        {
            "h9-loop": TaskProfile(
                name="h9-loop",
                skill_names=("my",),
                allowed_tools=frozenset(loop.tools.tool_names),
                output_contract=("complete",),
                dynamic_plugins=DynamicPluginPolicy(
                    enabled=True,
                    allowed_kinds=frozenset({"loop"}),
                    allowed_actions=frozenset({"activate"}),
                    allowed_scopes=frozenset({"session"}),
                ),
            )
        }
    )
    selection_session = loop.sessions.get_or_create("cli:direct")
    selection_session.metadata.update({"task_profile": "h9-loop", "task_profile_version": "1"})
    candidate = MagicMock(
        status="pending_confirmation",
        kind="loop",
        target_id="plan-then-act",
        candidate_version="1",
        evaluation_id="harness-evaluation-1",
        scope={
            "project_id": loop.plugin_manager.project_id_for_path(tmp_path),
            "workspace": str(tmp_path.resolve()),
        },
    )
    confirmation = MagicMock(
        confirmation_id="harness-confirmation-1",
        evaluation_id="harness-evaluation-1",
        scope={
            "project_id": loop.plugin_manager.project_id_for_path(tmp_path),
            "workspace": str(tmp_path.resolve()),
        },
    )
    selection = MagicMock()
    selection.get_candidate.return_value = candidate
    selection.get_confirmation.return_value = confirmation
    selection.issue_confirmation.return_value = confirmation
    selection.consume_confirmation.return_value = MagicMock(status="active")
    loop.harness_selection_store = selection

    prepared = await loop.process_direct(
        "/plugins selection prepare harness-candidate-1",
        session_key="cli:direct",
    )
    activated = await loop.process_direct(
        "/plugins selection confirm harness-confirmation-1 harness-evaluation-1",
        session_key="cli:direct",
    )

    assert prepared is not None and "Harness candidate is eligible" in prepared.content
    assert activated is not None and "active" in activated.content
    selection.issue_confirmation.assert_called_once()
    selection.consume_confirmation.assert_called_once()


@pytest.mark.asyncio
async def test_plugins_selection_rolls_back_the_claim_when_plugin_promotion_fails(
    tmp_path: Path,
) -> None:
    loop = _make_loop(tmp_path)
    project_id = loop.plugin_manager.project_id_for_path(tmp_path)
    loop.task_profiles = TaskProfileCatalog(
        {
            "h9-plugin": TaskProfile(
                name="h9-plugin",
                skill_names=("my",),
                allowed_tools=frozenset(loop.tools.tool_names),
                output_contract=("complete",),
                dynamic_plugins=DynamicPluginPolicy(
                    enabled=True,
                    allowed_kinds=frozenset({"tool"}),
                    allowed_actions=frozenset({"activate"}),
                    allowed_scopes=frozenset({"session"}),
                ),
            )
        }
    )
    selection_session = loop.sessions.get_or_create("cli:direct")
    selection_session.metadata.update({"task_profile": "h9-plugin", "task_profile_version": "1"})
    candidate = MagicMock(
        status="pending_confirmation",
        kind="plugin",
        target_id="model-tool",
        candidate_version="1.0.0",
        candidate_id="harness-candidate-plugin",
        scope={"project_id": project_id, "workspace": str(tmp_path.resolve())},
    )
    confirmation = MagicMock(
        confirmation_id="harness-confirmation-plugin",
        evaluation_id="harness-evaluation-plugin",
        scope={"project_id": project_id, "workspace": str(tmp_path.resolve())},
    )
    selection = MagicMock()
    selection.get_candidate.return_value = candidate
    selection.get_confirmation.return_value = confirmation
    selection.issue_confirmation.return_value = confirmation
    selection.consume_confirmation.return_value = MagicMock(
        status="active", candidate_id=candidate.candidate_id
    )
    loop.harness_selection_store = selection
    loop.plugin_manager.promote_candidate_to_project = MagicMock(
        return_value=MagicMock(ok=False, message="project copy failed")
    )

    await loop.process_direct(
        "/plugins selection prepare harness-candidate-plugin",
        session_key="cli:direct",
    )
    activated = await loop.process_direct(
        "/plugins selection confirm harness-confirmation-plugin harness-evaluation-plugin",
        session_key="cli:direct",
    )

    assert activated is not None and "activation stopped" in activated.content
    selection.consume_confirmation.assert_called_once()
    loop.plugin_manager.promote_candidate_to_project.assert_called_once()
    selection.rollback_confirmation_consumption.assert_called_once_with(
        "harness-confirmation-plugin", candidate_id=candidate.candidate_id
    )


def test_dynamic_view_without_authorization_is_the_original_registry() -> None:
    from nanobot.agent.plugins.manager import PluginManager

    base = ToolRegistry()
    manager = PluginManager(base)

    assert manager.tools_for_session(base, "cli:direct") is base
