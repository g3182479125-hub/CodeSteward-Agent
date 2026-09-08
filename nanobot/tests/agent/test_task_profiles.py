"""Public contract tests for trusted task profiles and loop detection."""

import json

import pytest

from nanobot.agent.task_profiles import (
    DEFAULT_TASK_PROFILES,
    TaskContextState,
    TaskLoopGuard,
    TaskProfileCatalog,
    TaskProfileError,
)
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry


class _StubTool(Tool):
    def __init__(self, tool_name: str) -> None:
        self._tool_name = tool_name

    @property
    def name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        return "test tool"

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}}

    async def execute(self, **_kwargs: object) -> str:
        return "executed"


def test_default_profiles_have_bounded_task_specific_tools() -> None:
    catalog = TaskProfileCatalog()

    assert set(catalog.names) == {"bug-fix", "feature-delivery", "repo-analysis"}
    assert catalog.get("repo-analysis").allowed_tools == frozenset({"devpilot_inspect_project"})
    assert "devpilot_confirmed_action" in catalog.get("feature-delivery").allowed_tools
    assert "devpilot_feature_delivery_propose" in catalog.get("feature-delivery").allowed_tools
    assert "devpilot_propose_changeset" not in catalog.get("feature-delivery").allowed_tools
    assert "devpilot_confirmed_action" in catalog.get("bug-fix").allowed_tools
    assert "shell" not in catalog.get("feature-delivery").allowed_tools


@pytest.mark.asyncio
async def test_feature_delivery_tool_view_blocks_legacy_and_keeps_contract_proposal(
    monkeypatch,
) -> None:
    from devpilot import nanobot_tools
    from devpilot.nanobot_tools import FeatureDeliveryProposeTool

    contract = {
        "change_set_id": "cs-1",
        "preview": {"contract_hash": "c" * 64, "check_ids": ["node-syntax"]},
    }
    monkeypatch.setattr(nanobot_tools, "_post_feature_proposal", lambda _payload: dict(contract))
    registry = ToolRegistry()
    registry.register(_StubTool("devpilot_propose_changeset"))
    registry.register(FeatureDeliveryProposeTool())

    view = registry.restricted_to(DEFAULT_TASK_PROFILES["feature-delivery"].allowed_tools)

    assert view.get("devpilot_feature_delivery_propose") is not None
    assert view.get("devpilot_propose_changeset") is None
    blocked = await view.execute("devpilot_propose_changeset", {})
    assert getattr(blocked, "is_error", False) is True
    result = await view.execute(
        "devpilot_feature_delivery_propose",
        {
            "project_path": "E:/project",
            "patches": [{"relative_path": "app.py", "operation": "create", "content": "ok\n"}],
            "evidence_ids": ["evidence-1"],
        },
    )
    payload = json.loads(str(result))
    assert payload["preview"]["contract_hash"] == contract["preview"]["contract_hash"]
    assert payload["preview"]["check_ids"] == contract["preview"]["check_ids"]


def test_profile_rejects_missing_tool_or_skill() -> None:
    profile = DEFAULT_TASK_PROFILES["feature-delivery"]

    with pytest.raises(TaskProfileError, match="tools="):
        TaskProfileCatalog().validate_runtime(
            profile,
            registered_tools={"devpilot_inspect_project"},
            skills={"feature-delivery"},
        )

    with pytest.raises(TaskProfileError, match="skills=feature-delivery"):
        TaskProfileCatalog().validate_runtime(
            profile,
            registered_tools=set(profile.allowed_tools),
            skills=set(),
        )


def test_task_loop_guard_pauses_after_three_identical_errors() -> None:
    guard = TaskLoopGuard()
    assert guard.observe("devpilot_read_code", {"path": "src/main.py"}, "Error: stale hash") is None
    assert guard.observe("devpilot_read_code", {"path": "src/main.py"}, "Error: stale hash") is None

    paused = guard.observe("devpilot_read_code", {"path": "src/main.py"}, "Error: stale hash")

    assert paused is not None
    assert "same tool error repeated" in paused
    assert "stale hash" in paused


def test_task_loop_guard_allows_one_new_diagnostic_branch() -> None:
    guard = TaskLoopGuard()
    assert guard.observe("tool", {"hypothesis": 1}, "Error: first") is None
    assert guard.observe("tool", {"hypothesis": 2}, "Error: second") is None
    assert guard.observe("tool", {"hypothesis": 2}, "Error: second") is None
    assert guard.observe("tool", {"hypothesis": 2}, "Error: second") is not None


def test_task_loop_guard_resets_consecutive_streak_after_success() -> None:
    guard = TaskLoopGuard()
    for _ in range(2):
        assert guard.observe("tool", {}, "Error: transient") is None

    guard.observe_success()

    assert guard.observe("tool", {}, "Error: transient") is None


def test_task_loop_guard_state_restores_for_the_same_profile_version() -> None:
    guard = TaskLoopGuard()
    for _ in range(2):
        assert guard.observe("tool", {}, "Error: persistent") is None

    restored = TaskLoopGuard.from_state(
        guard.to_state("feature-delivery@1"),
        "feature-delivery@1",
    )

    assert restored.observe("tool", {}, "Error: persistent") is not None
    fresh = TaskLoopGuard.from_state(guard.to_state("feature-delivery@1"), "bug-fix@1")
    assert fresh.observe("tool", {}, "Error: persistent") is None


def test_task_profile_version_is_part_of_the_session_binding() -> None:
    profile = TaskProfileCatalog().get("feature-delivery")

    assert profile.version == "2"


def test_task_context_state_is_structured_and_redacts_credentials() -> None:
    state = TaskContextState(
        session_ref="session-ref",
        project_ref="project-ref",
        profile_binding="feature-delivery@1",
        goal="fix api_key=secret-value in the parser",
        constraints=("keep scope",),
        completed=("inspection",),
        next_step="recheck hashes",
        run_id="run-1",
        evidence_refs=("evidence-1",),
    )

    stored = state.to_metadata()
    restored = TaskContextState.from_metadata(stored)

    assert restored is not None
    assert "secret-value" not in restored.goal
    assert restored.profile_binding == "feature-delivery@1"
    assert restored.run_id == "run-1"
