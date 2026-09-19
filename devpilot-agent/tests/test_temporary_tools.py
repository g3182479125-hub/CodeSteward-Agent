from __future__ import annotations

import json
import time

import pytest
from nanobot.agent.plugins.manager import PluginManager
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.security.workspace_access import bind_workspace_scope, build_workspace_scope

from devpilot import temporary_tools as temporary_tools_module
from devpilot.execution import ExecutionError, FakeExecutionBackend
from devpilot.temporary_tools import (
    TemporaryToolArtifactConflict,
    TemporaryToolArtifactStore,
    TemporaryToolContractError,
    TemporaryToolEnsureTool,
    TemporaryToolLifecycle,
    TemporaryToolManifest,
    TemporaryToolPersistTool,
    TemporaryToolState,
    TemporaryToolValidator,
    ToolCapabilityGap,
    bind_temporary_tool_manager,
    source_sha256,
    temporary_tools_allowed,
    validate_temporary_tool_source,
)


def _manifest(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "tool_id": "temporary-lookup",
        "version": "1.0.0",
        "session_id": "session-1",
        "run_id": "run-1",
        "project_id": "project-1",
        "name": "temporary_lookup",
        "description": "Look up a value for the current task.",
        "parameter_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "entry_module": "candidate.py",
        "entry_class": "TemporaryLookupTool",
        "source_hash": source_sha256("source"),
        "declared_capabilities": [],
        "created_at": 100,
        "expires_at": 200,
        "scope": "session",
    }
    raw.update(overrides)
    return raw


def test_manifest_round_trips_and_is_session_scoped() -> None:
    manifest = TemporaryToolManifest.from_mapping(_manifest())
    assert manifest.to_dict()["scope"] == "session"
    assert TemporaryToolManifest.from_mapping(manifest.to_dict()) == manifest


def test_artifact_store_freezes_a_tool_package_without_importing_it(tmp_path) -> None:
    source = """
from nanobot.agent.tools.base import Tool

class TemporaryLookupTool(Tool):
    name = "temporary_lookup"

    async def execute(self, value: str):
        return value
""".lstrip()
    manifest = TemporaryToolManifest.from_mapping(
        _manifest(source_hash=source_sha256(source), entry_class="TemporaryLookupTool")
    )
    artifact = TemporaryToolArtifactStore(tmp_path).write(manifest, source)
    assert artifact.source_path.read_text(encoding="utf-8") == source
    assert (artifact.root / "manifest.json").exists()
    assert TemporaryToolArtifactStore(tmp_path).write(manifest, source).root == artifact.root


def test_artifact_store_rejects_same_hash_path_with_different_content(tmp_path) -> None:
    source = "class TemporaryLookupTool(Tool):\n    async def execute(self):\n        return 1\n"
    manifest = TemporaryToolManifest.from_mapping(
        _manifest(source_hash=source_sha256(source), entry_class="TemporaryLookupTool")
    )
    store = TemporaryToolArtifactStore(tmp_path)
    artifact = store.write(manifest, source)
    artifact.source_path.write_text(source.replace("return 1", "return 2"), encoding="utf-8")
    with pytest.raises(TemporaryToolArtifactConflict):
        store.write(manifest, source)


def test_expired_pending_snapshot_is_deleted(tmp_path) -> None:
    source = "class TemporaryLookupTool(Tool):\n    async def execute(self):\n        return 1\n"
    manifest = TemporaryToolManifest.from_mapping(
        _manifest(
            source_hash=source_sha256(source),
            entry_class="TemporaryLookupTool",
            session_id="session-expired",
            run_id="run-expired",
            expires_at=int(time.time()) - 1,
        )
    )
    store = TemporaryToolArtifactStore(tmp_path)
    artifact = store.write(manifest, source)

    assert store.pending_for_session("session-expired") == ()
    assert not artifact.root.exists()


def test_source_validator_checks_shape_without_running_code() -> None:
    valid = "class DemoTool(Tool):\n    async def execute(self):\n        return 1\n"
    validate_temporary_tool_source(valid, "DemoTool")
    with pytest.raises(TemporaryToolContractError, match="inherit Tool"):
        validate_temporary_tool_source(
            "class DemoTool:\n    async def execute(self):\n        return 1\n", "DemoTool"
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scope", "project"),
        ("entry_module", "../outside.py"),
        ("entry_module", "C:/outside.py"),
        ("source_hash", "not-a-hash"),
    ],
)
def test_manifest_rejects_untrusted_loading_fields(field: str, value: object) -> None:
    with pytest.raises(TemporaryToolContractError):
        TemporaryToolManifest.from_mapping(_manifest(**{field: value}))


def test_manifest_rejects_unknown_fields() -> None:
    with pytest.raises(TemporaryToolContractError, match="unknown"):
        TemporaryToolManifest.from_mapping(_manifest(unexpected="value"))


def test_capability_gap_is_bound_to_one_session_and_run() -> None:
    gap = ToolCapabilityGap(
        session_id="session-1",
        run_id="run-1",
        project_id="project-1",
        reason="现有工具无法查询内部服务的健康状态",
        requested_capability="health_lookup",
        existing_tools=("devpilot_read_code", "devpilot_test_run"),
        suggested_tool_id="health-lookup",
    )
    assert gap.to_dict()["session_id"] == "session-1"
    assert gap.to_dict()["existing_tools"] == ["devpilot_read_code", "devpilot_test_run"]


def test_lifecycle_accepts_valid_path_and_rejects_skipping_validation() -> None:
    lifecycle = TemporaryToolLifecycle(TemporaryToolState.GAP_DETECTED)
    for state in (
        TemporaryToolState.GENERATED,
        TemporaryToolState.VALIDATING,
        TemporaryToolState.VALIDATED,
        TemporaryToolState.ACTIVE,
        TemporaryToolState.RETIRING,
        TemporaryToolState.DISPOSED,
    ):
        lifecycle.transition(state)
    assert lifecycle.to_dict()["history"][-1] == "disposed"
    invalid = TemporaryToolLifecycle(TemporaryToolState.GENERATED)
    with pytest.raises(TemporaryToolContractError, match="invalid"):
        invalid.transition(TemporaryToolState.ACTIVE)


@pytest.mark.parametrize(
    ("access_mode", "channel", "expected"),
    [("full", "websocket", True), ("restricted", "websocket", False), ("full", "cli", False)],
)
def test_temporary_tools_require_full_access_websocket(
    access_mode: str, channel: str, expected: bool
) -> None:
    assert temporary_tools_allowed(access_mode, source_channel=channel) is expected


def test_fc_validator_sends_only_the_frozen_artifact_and_contract_checker(tmp_path) -> None:
    source = """
from nanobot.agent.tools.base import Tool

class TemporaryLookupTool(Tool):
    async def execute(self):
        return 1
""".lstrip()
    manifest = TemporaryToolManifest.from_mapping(
        _manifest(source_hash=source_sha256(source), entry_class="TemporaryLookupTool")
    )
    artifact = TemporaryToolArtifactStore(tmp_path).write(manifest, source)
    backend = FakeExecutionBackend()
    result = TemporaryToolValidator(backend).validate(artifact)
    assert result.status == "validated"
    assert result.receipt is not None
    assert backend.requests[0].command == "python /workspace/devpilot/contract_check.py"
    assert {item.relative_path for item in backend.requests[0].files} == {
        "candidate.py",
        "manifest.json",
        "contract_check.py",
        "nanobot/__init__.py",
        "nanobot/agent/__init__.py",
        "nanobot/agent/tools/__init__.py",
        "nanobot/agent/tools/base.py",
    }
    assert backend.requests[0].network_allowed is False


def test_fc_validator_maps_timeout_without_activation(tmp_path) -> None:
    source = "class TemporaryLookupTool(Tool):\n    async def execute(self):\n        return 1\n"
    manifest = TemporaryToolManifest.from_mapping(
        _manifest(source_hash=source_sha256(source), entry_class="TemporaryLookupTool")
    )
    artifact = TemporaryToolArtifactStore(tmp_path).write(manifest, source)

    class TimeoutBackend:
        name = "fc-agent-sandbox"

        def run(self, request):
            del request
            raise ExecutionError("FC_TIMEOUT", "sandbox timed out")

    result = TemporaryToolValidator(TimeoutBackend()).validate(artifact)

    assert result.status == TemporaryToolState.REJECTED.value
    assert result.error_code == "FC_TIMEOUT"
    assert result.receipt is None


@pytest.mark.asyncio
async def test_ensure_tool_runs_create_validate_and_hot_load_chain(tmp_path, monkeypatch) -> None:
    backend = FakeExecutionBackend()
    monkeypatch.setattr(
        temporary_tools_module,
        "build_fc_backend_from_environment",
        lambda: backend,
    )
    manager = PluginManager(ToolRegistry())
    bind_temporary_tool_manager(manager)
    scope_token = bind_workspace_scope(
        build_workspace_scope(tmp_path, "full", source_channel="websocket")
    )
    request = RequestContext(
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
        sender_id="user-1",
        workspace=tmp_path,
        attributes={"devpilot_run_id": "run-1", "devpilot_project_id": "project-1"},
    )
    source = """
from nanobot.agent.tools.base import Tool, tool_parameters

@tool_parameters(
    {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }
)
class TemporaryLookupTool(Tool):
    @property
    def name(self):
        return "temporary_lookup"

    @property
    def description(self):
        return "temporary"

    async def execute(self, value):
        return value
""".lstrip()
    try:
        with request_context(request):
            result = await TemporaryToolEnsureTool(tmp_path).execute(
                tool_id="temporary-lookup",
                version="1.0.0",
                name="temporary_lookup",
                description="temporary",
                entry_class="TemporaryLookupTool",
                source=source,
                parameter_schema={"type": "object"},
                output_schema={"type": "object"},
            )
        payload = json.loads(str(result))
        assert payload["ok"] is True
        assert payload["stages"] == {
            "create": "generated",
            "validate": "validated",
            "activate": "active",
        }
        assert backend.requests[0].command == "python /workspace/devpilot/contract_check.py"
        view = manager.live_tools_for_session(manager._base_registry, "websocket:chat-1")
        assert "temporary_lookup" in view.tool_names
        cleanup = await manager.cleanup_temporary_run(
            session_id="websocket:chat-1",
            run_id="run-1",
        )
        assert cleanup[0]["artifact"] == "pending_persistence"
        with request_context(request):
            persisted = await TemporaryToolPersistTool(tmp_path).execute()
        persisted_payload = json.loads(str(persisted))
        assert persisted_payload["ok"] is True
        persisted_view = manager.live_tools_for_session(
            manager._base_registry,
            "websocket:chat-1",
            project_id="project-1",
        )
        assert "temporary_lookup" in persisted_view.tool_names
    finally:
        from nanobot.security.workspace_access import reset_workspace_scope

        reset_workspace_scope(scope_token)
