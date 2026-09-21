from nanobot.agent.turn_outcomes import (
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
