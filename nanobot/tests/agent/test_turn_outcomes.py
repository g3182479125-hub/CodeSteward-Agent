from nanobot.agent.turn_outcomes import (
    classify_exception,
    classify_turn_outcome,
    message_for_outcome,
)


def test_guard_budget_stops_only_the_current_turn() -> None:
    outcome = classify_turn_outcome(
        "driver_paused",
        "bounded diagnostic-branch budget exhausted",
    )

    assert outcome.category == "guard_stopped"
    assert outcome.scope == "turn"
    assert outcome.continuation == "allow"
    assert outcome.retry_hint == "change_input"
    assert "Task paused" not in message_for_outcome(outcome)
    assert "diagnostic" not in message_for_outcome(outcome)


def test_path_boundary_is_safe_but_does_not_lock_the_session() -> None:
    outcome = classify_turn_outcome(
        "tool_error",
        "PathBoundaryError: path is outside the allowed roots",
    )

    assert outcome.category == "blocked_for_safety"
    assert outcome.continuation == "allow"
    assert outcome.retry_hint == "change_input"
    assert "PathBoundaryError" not in message_for_outcome(outcome)


def test_unknown_side_effect_is_limited_to_querying_that_operation() -> None:
    outcome = classify_turn_outcome(
        "driver_paused",
        "uncertain_side_effect_requires_recovery",
        operation_refs=("run:123",),
    )

    assert outcome.category == "unknown_side_effect"
    assert outcome.scope == "operation"
    assert outcome.continuation == "allow_readonly"
    assert outcome.retry_hint == "check_status"
    assert outcome.to_metadata()["operation_refs"] == ["run:123"]
    assert "internal_code" not in outcome.to_metadata()


def test_unknown_side_effect_reference_wins_over_generic_driver_pause() -> None:
    outcome = classify_turn_outcome("driver_paused", operation_refs=("run:456",))

    assert outcome.category == "unknown_side_effect"
    assert outcome.continuation == "allow_readonly"
    assert outcome.retry_hint == "check_status"


def test_driver_finish_is_a_completed_turn() -> None:
    assert classify_turn_outcome("driver_finished").category == "completed"


def test_exception_source_is_classified_without_exposing_details() -> None:
    class DevPilotRuntimeError(RuntimeError):
        pass

    class ProviderRuntimeError(RuntimeError):
        pass

    DevPilotRuntimeError.__module__ = "devpilot.nanobot_tools"

    assert classify_exception(ProviderRuntimeError("unrelated detail")) == "model_error"
    assert classify_exception(RuntimeError("worker failed"), sender_id="subagent") == "subagent_error"
    assert classify_exception(RuntimeError("DevPilot project inspection failed")) == "tool_error"
    assert classify_exception(RuntimeError("TaskProfileError: profile unavailable")) == "tool_error"
    assert classify_exception(DevPilotRuntimeError("database schema is not initialized")) == "devpilot_error"


def test_project_failure_sources_keep_recovery_scope_and_user_copy_separate() -> None:
    devpilot = classify_turn_outcome("devpilot_error")
    task_profile = classify_turn_outcome("task_profile_error")
    subagent = classify_turn_outcome("subagent_error")

    assert devpilot.user_message_key == "devpilot.failed"
    assert task_profile.user_message_key == "devpilot.failed"
    assert subagent.user_message_key == "subagent.failed"
    assert all(item.continuation == "allow" for item in (devpilot, task_profile, subagent))


def test_outcome_metadata_does_not_leak_internal_detail() -> None:
    outcome = classify_turn_outcome("tool_error", "secret stack trace")

    assert outcome.to_metadata() == {
        "schema_version": 1,
        "category": "recoverable_failure",
        "scope": "turn",
        "message_key": "tool.failed",
        "retry_hint": "retry_new",
        "continuation": "allow",
        "operation_refs": [],
    }
