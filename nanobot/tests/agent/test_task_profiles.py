"""Public contract tests for trusted task profiles and loop detection."""

import pytest

from nanobot.agent.task_profiles import (
    DEFAULT_TASK_PROFILES,
    TaskLoopGuard,
    TaskProfileCatalog,
    TaskProfileError,
)


def test_default_profiles_have_bounded_task_specific_tools() -> None:
    catalog = TaskProfileCatalog()

    assert set(catalog.names) == {"bug-fix", "feature-delivery", "repo-analysis"}
    assert catalog.get("repo-analysis").allowed_tools == frozenset({
        "devpilot_inspect_project",
        "devpilot_publish_artifact",
    })
    assert "devpilot_confirmed_action" in catalog.get("feature-delivery").allowed_tools
    assert "devpilot_confirmed_action" in catalog.get("bug-fix").allowed_tools
    assert "shell" not in catalog.get("feature-delivery").allowed_tools


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


def test_task_profile_version_is_part_of_the_session_binding() -> None:
    profile = TaskProfileCatalog().get("feature-delivery")

    assert profile.version == "1"
