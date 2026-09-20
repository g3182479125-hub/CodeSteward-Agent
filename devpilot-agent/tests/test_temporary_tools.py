from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from nanobot.agent.plugins.manager import PluginManager
from nanobot.agent.tools.context import RequestContext, request_context
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.security.workspace_access import bind_workspace_scope, build_workspace_scope

from devpilot import temporary_tools as temporary_tools_module
from devpilot.execution import ExecutionError, ExecutionResult, FakeExecutionBackend
from devpilot.temporary_tools import (
    TemporaryToolArtifactConflict,
    TemporaryToolArtifactStore,
    TemporaryToolContractError,
    TemporaryToolEnsureTool,
    TemporaryToolGapReportTool,
    TemporaryToolLifecycle,
    TemporaryToolManifest,
    TemporaryToolPersistTool,
    TemporaryToolState,
    TemporaryToolValidator,
    ToolCapabilityGap,
    ToolCapabilityGapStore,
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
    lifecycle = TemporaryToolArtifactStore(tmp_path).load_lifecycle(artifact)
    assert lifecycle.state is TemporaryToolState.GENERATED
    assert json.loads((artifact.root / "lifecycle.json").read_text()) == {
        "history": ["generated"],
        "state": "generated",
    }
    assert TemporaryToolArtifactStore(tmp_path).write(manifest, source).root == artifact.root


def test_lifecycle_sidecar_round_trips_and_rejects_invalid_history(tmp_path) -> None:
    source = "class TemporaryLookupTool(Tool):\n    async def execute(self):\n        return 1\n"
    manifest = TemporaryToolManifest.from_mapping(
        _manifest(source_hash=source_sha256(source), entry_class="TemporaryLookupTool")
    )
    store = TemporaryToolArtifactStore(tmp_path)
    artifact = store.write(manifest, source)
    store.transition_lifecycle(artifact, TemporaryToolState.VALIDATING)
    store.transition_lifecycle(artifact, TemporaryToolState.REJECTED)
    assert store.load_lifecycle(artifact).to_dict() == {
        "state": "rejected",
        "history": ["generated", "validating", "rejected"],
    }
    (artifact.root / "lifecycle.json").write_text(
        json.dumps({"state": "active", "history": ["generated", "active"]}),
        encoding="utf-8",
    )
    with pytest.raises(TemporaryToolContractError, match="transition"):
        store.load_lifecycle(artifact)


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


def test_capability_gap_store_persists_redacted_gap_before_candidate(tmp_path) -> None:
    gap = ToolCapabilityGap(
        session_id="session-1",
        run_id="run-1",
        project_id="project-1",
        reason="现有工具没有 import graph 能力",
        requested_capability="import_graph",
        existing_tools=("devpilot_read_code",),
        suggested_tool_id="import-graph",
    )
    store = ToolCapabilityGapStore(tmp_path)
    path = store.write(gap)
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "gap_detected"
    assert store.has_for_run(session_id="session-1", run_id="run-1") is True
    assert TemporaryToolGapReportTool().name == "devpilot_report_tool_gap"


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


def test_temporary_tools_reject_workspace_restricted_full_access() -> None:
    assert (
        temporary_tools_allowed(
            "full",
            source_channel="websocket",
            restrict_to_workspace=True,
        )
        is False
    )


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
    assert result.execution_run_id == backend.requests[0].run_id
    receipt = json.loads((artifact.root / "validation.json").read_text(encoding="utf-8"))
    assert receipt["validation_version"] == "temporary-tool-validation/v1"
    assert receipt["status"] == TemporaryToolState.VALIDATED.value
    assert receipt["execution_run_id"] == backend.requests[0].run_id
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


def test_fc_contract_checker_executes_candidate_and_reports_controlled_fixture(tmp_path) -> None:
    source = """
from nanobot.agent.tools.base import Tool, tool_parameters

@tool_parameters({"type": "object", "properties": {}, "additionalProperties": False})
class TemporaryLookupTool(Tool):
    @property
    def name(self):
        return "temporary_lookup"

    @property
    def description(self):
        return "fixture"

    async def execute(self):
        return {}
""".lstrip()
    manifest = TemporaryToolManifest.from_mapping(
        _manifest(
            source_hash=source_sha256(source),
            entry_class="TemporaryLookupTool",
            parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
        )
    )
    artifact = TemporaryToolArtifactStore(tmp_path).write(manifest, source)

    class SubprocessExecutionBackend:
        name = "local-contract-fixture"

        def run(self, request):
            root = tmp_path / "fc-workspace"
            for item in request.files:
                path = root / item.relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(item.content)
            completed = subprocess.run(
                [sys.executable, str(root / "contract_check.py")],
                cwd=root,
                env={**os.environ, "DEVPILOT_CONTRACT_ROOT": str(root)},
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                check=False,
            )
            return ExecutionResult(
                backend=self.name,
                sandbox_id="local-contract-fixture",
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                duration_ms=0,
                command_profile=request.command_profile,
                side_effect_count=0,
                permission_violation=False,
            )

    result = TemporaryToolValidator(SubprocessExecutionBackend()).validate(artifact)
    assert result.status == "validated", (
        result.error_code,
        result.receipt.stdout if result.receipt else None,
        result.receipt.stderr if result.receipt else None,
    )
    assert result.receipt is not None
    fixture_output = json.loads(result.receipt.stdout)
    assert fixture_output["fixtures"] == {
        "controlled_error": True,
        "invalid_params": True,
        "success": True,
    }


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
        artifact = TemporaryToolArtifactStore(tmp_path).load_for_session(
            session_id="websocket:chat-1",
            run_id="run-1",
            tool_id="temporary-lookup",
            version="1.0.0",
            source_hash=payload["source_hash"],
        )
        assert TemporaryToolArtifactStore(tmp_path).load_lifecycle(artifact).state is (
            TemporaryToolState.ACTIVE
        )
        assert backend.requests[0].command == "python /workspace/devpilot/contract_check.py"
        assert len(backend.requests) == 2
        assert backend.requests[0].run_id != backend.requests[1].run_id
        view = manager.live_tools_for_session(
            manager._base_registry, "websocket:chat-1", project_id="project-1"
        )
        assert "temporary_lookup" in view.tool_names
        cleanup = await manager.cleanup_temporary_run(
            session_id="websocket:chat-1",
            run_id="run-1",
        )
        assert cleanup[0]["artifact"] == "pending_persistence"
        assert TemporaryToolArtifactStore(tmp_path).load_lifecycle(artifact).state is (
            TemporaryToolState.RETIRING
        )
        TemporaryToolArtifactStore(tmp_path).transition_lifecycle(
            artifact, TemporaryToolState.PERSISTENCE_REQUESTED
        )
        with request_context(request):
            pending = await TemporaryToolPersistTool(tmp_path).execute()
        assert json.loads(str(pending))["code"] == "USER_CONFIRMATION_REQUIRED"
        issued_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        expires_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat().replace(
            "+00:00", "Z"
        )
        binding = {
            "session_id": "websocket:chat-1",
            "run_id": "run-1",
            "project_id": "project-1",
            "tool_id": "temporary-lookup",
            "version": "1.0.0",
            "source_hash": payload["source_hash"],
        }
        context = {
            **binding,
            "capability": "persist_temporary_tool",
            "confirmation_id": "temporary-confirmation-1",
            "issued_at": issued_at,
            "expires_at": expires_at,
            "user_event_nonce": "temporary-nonce-1",
        }
        canonical = "\n".join(
            (
                "temporary-tool-confirmation-v1",
                "temporary_tool_persist",
                binding["session_id"],
                binding["run_id"],
                binding["project_id"],
                binding["tool_id"],
                binding["version"],
                binding["source_hash"],
                context["capability"],
                context["confirmation_id"],
                context["issued_at"],
                context["expires_at"],
                context["user_event_nonce"],
            )
        )
        key = b"temporary-confirmation-test-key"
        context["signature"] = hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()
        request.attributes["devpilot_confirmation"] = {
            "action": "temporary_tool_persist",
            "context": context,
            "verifier": lambda value, signature: hmac.compare_digest(
                hmac.new(key, value.encode(), hashlib.sha256).hexdigest(), signature
            ),
        }
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


def test_source_validator_rejects_undeclared_sensitive_capabilities() -> None:
    source = """
import os

class DemoTool(Tool):
    async def execute(self):
        return os.getenv("TOKEN")
""".lstrip()
    with pytest.raises(TemporaryToolContractError, match="undeclared capabilities"):
        validate_temporary_tool_source(source, "DemoTool")
    validate_temporary_tool_source(
        source,
        "DemoTool",
        declared_capabilities=["privacy_read"],
    )


def test_workspace_file_reads_are_not_privacy_reads_but_environment_is() -> None:
    workspace_source = """
from pathlib import Path

class DemoTool(Tool):
    async def execute(self):
        return Path('README.md').read_text()
""".lstrip()
    validate_temporary_tool_source(
        workspace_source, "DemoTool", declared_capabilities=["sensitive_read"]
    )
    environment_source = """
import os

class DemoTool(Tool):
    async def execute(self):
        return os.getenv('TOKEN')
""".lstrip()
    with pytest.raises(TemporaryToolContractError, match="privacy_read"):
        validate_temporary_tool_source(environment_source, "DemoTool")


def test_literal_workspace_escape_is_privacy_gated() -> None:
    source = """
from pathlib import Path

class DemoTool(Tool):
    async def execute(self):
        return Path('C:/Users/secret.txt').read_text()
""".lstrip()
    with pytest.raises(TemporaryToolContractError, match="privacy_read"):
        validate_temporary_tool_source(source, "DemoTool")


def test_parent_and_runtime_computed_paths_are_privacy_gated() -> None:
    parent_source = """
from pathlib import Path

class DemoTool(Tool):
    async def execute(self):
        return Path('../secret.txt').read_text()
""".lstrip()
    with pytest.raises(TemporaryToolContractError, match="privacy_read"):
        validate_temporary_tool_source(parent_source, "DemoTool")

    runtime_source = """
class DemoTool(Tool):
    async def execute(self, path):
        return open(path).read()
""".lstrip()
    with pytest.raises(TemporaryToolContractError, match="privacy_read"):
        validate_temporary_tool_source(runtime_source, "DemoTool")

    external_source = """
import subprocess

class DemoTool(Tool):
    async def execute(self):
        return subprocess.run(['echo', 'ok'])
""".lstrip()
    with pytest.raises(TemporaryToolContractError, match="external_send"):
        validate_temporary_tool_source(external_source, "DemoTool")


def test_capability_gap_persistence_redacts_credential_shaped_text(tmp_path: Path) -> None:
    gap = temporary_tools_module.ToolCapabilityGap(
        session_id="session-1",
        run_id="run-1",
        project_id="project-1",
        reason="need api_key=super-secret and bearer abc123",
        requested_capability="token=another-secret",
    )
    record = json.loads(
        temporary_tools_module.ToolCapabilityGapStore(tmp_path).write(gap).read_text(
            encoding="utf-8"
        )
    )
    assert "super-secret" not in json.dumps(record)
    assert "another-secret" not in json.dumps(record)
    assert "[REDACTED]" in record["reason"]


def test_validation_receipt_redacts_provider_output(tmp_path: Path) -> None:
    source = "class TemporaryLookupTool(Tool):\n    async def execute(self):\n        return {}\n"
    manifest = TemporaryToolManifest.from_mapping(
        _manifest(source_hash=source_sha256(source), entry_class="TemporaryLookupTool")
    )
    artifact = TemporaryToolArtifactStore(tmp_path).write(manifest, source)
    result = TemporaryToolValidator(
        FakeExecutionBackend(stdout='{"token": "provider-secret"}')
    ).validate(artifact)
    receipt = json.loads((artifact.root / "validation.json").read_text(encoding="utf-8"))
    assert "provider-secret" not in json.dumps(receipt)
    assert result.status == "validated"


@pytest.mark.skipif(
    not os.environ.get("DEVPILOT_RUN_LIVE_FC"),
    reason="live FC evidence is opt-in",
)
def test_fc_validator_final_checker_live_backend(tmp_path: Path) -> None:
    """Run the final checker version once through the configured FC provider."""
    source = """
from nanobot.agent.tools.base import Tool, tool_parameters

@tool_parameters({"type": "object", "properties": {}, "additionalProperties": False})
class TemporaryLookupTool(Tool):
    @property
    def name(self):
        return "temporary_lookup"

    @property
    def description(self):
        return "live FC fixture"

    async def execute(self):
        return {}
""".lstrip()
    manifest = TemporaryToolManifest.from_mapping(
        _manifest(
            source_hash=source_sha256(source),
            entry_class="TemporaryLookupTool",
            parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
        )
    )
    artifact = TemporaryToolArtifactStore(tmp_path).write(manifest, source)
    result = TemporaryToolValidator(
        temporary_tools_module.build_fc_backend_from_environment()
    ).validate(artifact)
    assert result.status == "validated"
    assert result.receipt is not None
    fixtures = json.loads(result.receipt.stdout)["fixtures"]
    assert fixtures == {
        "controlled_error": True,
        "invalid_params": True,
        "success": True,
    }


def test_full_access_workspace_reads_do_not_trigger_privacy_confirmation() -> None:
    assert "sensitive_read" not in temporary_tools_module._SENSITIVE_CAPABILITIES
    assert "privacy_read" in temporary_tools_module._SENSITIVE_CAPABILITIES
