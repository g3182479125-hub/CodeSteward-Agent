from __future__ import annotations

import pytest

from devpilot.temporary_tools import (
    TemporaryToolContractError,
    TemporaryToolLifecycle,
    TemporaryToolManifest,
    TemporaryToolState,
    ToolCapabilityGap,
    source_sha256,
    temporary_tools_allowed,
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
    lifecycle.transition(TemporaryToolState.GENERATED)
    lifecycle.transition(TemporaryToolState.VALIDATING)
    lifecycle.transition(TemporaryToolState.VALIDATED)
    lifecycle.transition(TemporaryToolState.ACTIVE)
    lifecycle.transition(TemporaryToolState.RETIRING)
    lifecycle.transition(TemporaryToolState.DISPOSED)

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

