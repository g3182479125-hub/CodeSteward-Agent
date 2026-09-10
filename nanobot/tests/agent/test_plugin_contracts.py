from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from nanobot.agent.plugins import (
    PluginCandidateStatus,
    PluginCatalog,
    PluginContractError,
    PluginErrorCode,
    PluginExecutionMode,
    PluginKind,
    PluginLifecycle,
    PluginManifest,
    PluginOperationResult,
    PluginRecord,
    PluginSource,
)


def _manifest(
    plugin_id: str,
    *,
    version: str = "1.0.0",
    entry: str = "tool.py",
    scope: str = "session",
    execution_mode: str = "in_process",
) -> PluginManifest:
    return PluginManifest.from_mapping(
        {
            "plugin_id": plugin_id,
            "version": version,
            "content_hash": "a" * 64,
            "kind": "tool",
            "api_version": "nanobot.tool/v1",
            "scope": scope,
            "entry": entry,
            "requires": [],
            "provides": [f"tool:{plugin_id}"],
            "requested_capabilities": ["read_only"],
            "execution_mode": execution_mode,
        }
    )


def test_manifest_round_trips_two_independent_tool_implementations() -> None:
    first = _manifest("demo-greeting")
    second = _manifest("demo-math", entry="math_tool.py")

    assert first.kind is PluginKind.TOOL
    assert second.entry == "math_tool.py"
    assert PluginManifest.from_mapping(first.to_dict()) == first
    assert PluginManifest.from_mapping(second.to_dict()) == second


def test_manifest_rejects_unknown_fields_and_unsafe_entries() -> None:
    raw = _manifest("safe-tool").to_dict()
    raw["unexpected"] = True
    with pytest.raises(PluginContractError) as unknown:
        PluginManifest.from_mapping(raw)
    assert unknown.value.code.value == "unknown_manifest_field"

    unsafe = _manifest("unsafe-tool").to_dict()
    unsafe["entry"] = "../escape.py"
    with pytest.raises(PluginContractError) as escaped:
        PluginManifest.from_mapping(unsafe)
    assert escaped.value.code.value == "invalid_entry"

    nul = _manifest("nul-tool").to_dict()
    nul["entry"] = "tool\x00.py"
    with pytest.raises(PluginContractError) as control:
        PluginManifest.from_mapping(nul)
    assert control.value.code.value == "invalid_entry"


def test_manifest_resolves_only_inside_artifact(tmp_path) -> None:
    manifest = _manifest("bounded-tool")
    entry = tmp_path / "tool.py"
    entry.write_text("# fixture\n", encoding="utf-8")

    assert manifest.resolve_entry(tmp_path) == entry.resolve()


def test_catalog_rejects_reserved_override_and_duplicate_version() -> None:
    catalog = PluginCatalog(
        session_id="session-a",
        reserved_capabilities={"tool:core.read"},
    )
    reserved = PluginManifest.from_mapping(
        {
            **_manifest("replacement").to_dict(),
            "provides": ["tool:core.read"],
        }
    )
    with pytest.raises(PluginContractError) as conflict:
        catalog.register(PluginRecord(reserved, PluginSource.DEVELOPER, session_id="session-a"))
    assert conflict.value.code.value == "reserved_capability"

    record = PluginRecord(_manifest("custom-tool"), PluginSource.DEVELOPER, session_id="session-a")
    catalog.register(record)
    with pytest.raises(PluginContractError) as duplicate:
        catalog.register(record)
    assert duplicate.value.code.value == "duplicate_version"


def test_catalog_can_derive_reserved_names_from_the_live_registry() -> None:
    class RegistryFixture:
        @property
        def tool_names(self) -> tuple[str, ...]:
            return ("core_read",)

    catalog = PluginCatalog.from_registry(
        session_id="session-a",
        registry=RegistryFixture(),
    )
    reserved = PluginManifest.from_mapping(
        {
            **_manifest("registry-replacement").to_dict(),
            "provides": ["tool:core_read"],
        }
    )

    with pytest.raises(PluginContractError) as conflict:
        catalog.register(PluginRecord(reserved, PluginSource.DEVELOPER, session_id="session-a"))
    assert conflict.value.code is PluginErrorCode.RESERVED_CAPABILITY


def test_record_scope_binding_errors_are_machine_readable() -> None:
    with pytest.raises(PluginContractError) as missing:
        PluginRecord(_manifest("missing-session"), PluginSource.DEVELOPER)
    assert missing.value.code is PluginErrorCode.SCOPE_MISMATCH

    with pytest.raises(PluginContractError) as double_bound:
        PluginRecord(
            _manifest("double-bound"),
            PluginSource.DEVELOPER,
            session_id="session-a",
            project_id="project-a",
        )
    assert double_bound.value.code is PluginErrorCode.SCOPE_MISMATCH


def test_catalog_isolates_sessions_and_exposes_read_only_status() -> None:
    first = PluginCatalog(session_id="session-a", reserved_capabilities=())
    second = PluginCatalog(session_id="session-b", reserved_capabilities=())
    record = PluginRecord(
        _manifest("session-tool"),
        PluginSource.DEVELOPER,
        session_id="session-a",
        online_lifecycle=True,
    )
    first.register(record)

    assert [item["plugin_id"] for item in first.list_view()] == ["session-tool"]
    assert second.list_view() == ()
    assert first.status("session-tool", "1.0.0") == {
        "plugin_id": "session-tool",
        "version": "1.0.0",
        "source": "developer",
        "scope": "session",
        "lifecycle": "inactive",
        "online_lifecycle": True,
        "content_hash": "a" * 64,
    }
    assert second.status("session-tool", "1.0.0") is None

    with pytest.raises(PluginContractError) as wrong_scope:
        second.register(record)
    assert wrong_scope.value.code.value == "scope_mismatch"


def test_candidate_status_is_separate_from_runtime_lifecycle() -> None:
    catalog = PluginCatalog(session_id="session-a", reserved_capabilities=())
    candidate = PluginRecord(
        _manifest("candidate-tool", execution_mode=PluginExecutionMode.FC_PROXY.value),
        PluginSource.MODEL_CANDIDATE,
        session_id="session-a",
        candidate_status=PluginCandidateStatus.TESTED,
    )
    catalog.register(candidate)

    status = catalog.status("candidate-tool", "1.0.0")
    assert status is not None
    assert status["candidate_status"] == "tested"
    assert status["lifecycle"] == "inactive"

    with pytest.raises(PluginContractError) as active_candidate:
        PluginRecord(
            _manifest("active-candidate"),
            PluginSource.MODEL_CANDIDATE,
            session_id="session-a",
            lifecycle=PluginLifecycle.ACTIVE,
            candidate_status=PluginCandidateStatus.TESTED,
        )
    assert active_candidate.value.code.value == "model_candidate_not_active"

    with pytest.raises(PluginContractError) as local_candidate:
        PluginRecord(
            _manifest("local-candidate", execution_mode=PluginExecutionMode.IN_PROCESS.value),
            PluginSource.MODEL_CANDIDATE,
            session_id="session-a",
            candidate_status=PluginCandidateStatus.TESTED,
        )
    assert local_candidate.value.code.value == "model_candidate_not_fc_proxy"


@pytest.mark.asyncio
async def test_example_manifests_point_to_two_independent_implementations() -> None:
    root = Path(__file__).parents[3]
    example_root = root / "devpilot-agent" / "examples" / "pluggable-tool"
    manifest_paths = (example_root / "plugin-manifest.json", example_root / "plugin-manifest.math.json")

    manifests = [
        PluginManifest.from_mapping(json.loads(path.read_text(encoding="utf-8")))
        for path in manifest_paths
    ]
    entries = [manifest.resolve_entry(example_root) for manifest in manifests]

    assert [manifest.plugin_id for manifest in manifests] == ["devflow-greeting", "devflow-math"]
    assert all(entry.is_file() for entry in entries)
    assert all(
        hashlib.sha256(entry.read_bytes()).hexdigest() == manifest.content_hash
        for entry, manifest in zip(entries, manifests, strict=True)
    )

    loaded = []
    for index, (entry, class_name) in enumerate(
        zip(entries, ("WelcomeTool", "AdditionTool"), strict=True)
    ):
        module_spec = importlib.util.spec_from_file_location(f"h1_example_{index}", entry)
        assert module_spec is not None and module_spec.loader is not None
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        loaded.append(getattr(module, class_name)())

    assert await loaded[0].execute(name="小明") == "插件工具已加载：你好，小明！"
    assert await loaded[1].execute(left=2, right=3) == 5


def test_operation_result_is_stable_and_json_compatible() -> None:
    result = PluginOperationResult.failure(
        code=PluginErrorCode.SCOPE_MISMATCH,
        message="plugin is not visible in this session",
        plugin_id="demo",
        details={"scope": "session"},
    )

    assert json.loads(json.dumps(result.to_dict()))["code"] == "scope_mismatch"
    assert result.ok is False
