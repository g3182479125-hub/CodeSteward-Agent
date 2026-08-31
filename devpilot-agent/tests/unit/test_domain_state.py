from datetime import UTC, datetime

import pytest

from devpilot.domain import (
    Approval,
    ApprovalStatus,
    ChangePlan,
    ChangePlanStatus,
    ChangeSet,
    ChangeSetStatus,
    DomainStateError,
    Task,
    TaskStatus,
)


def test_task_follows_planning_lifecycle() -> None:
    task = Task(project_id="project-1", request_text="Add a feature")

    task.transition_to(TaskStatus.PLANNING)
    task.transition_to(TaskStatus.WAITING_APPROVAL)

    assert task.status is TaskStatus.WAITING_APPROVAL


def test_task_rejects_skipping_approval() -> None:
    task = Task(project_id="project-1", request_text="Add a feature")

    with pytest.raises(DomainStateError) as error:
        task.transition_to(TaskStatus.APPLYING)

    assert error.value.code == "INVALID_STATE_TRANSITION"
    assert error.value.current == "draft"
    assert error.value.target == "applying"


def test_task_terminal_state_cannot_restart() -> None:
    task = Task(project_id="project-1", request_text="Add a feature")
    task.transition_to(TaskStatus.CANCELLED)

    with pytest.raises(DomainStateError):
        task.transition_to(TaskStatus.PLANNING)


def test_change_plan_can_be_frozen_then_superseded() -> None:
    plan = ChangePlan(
        task_id="task-1",
        version=1,
        plan_hash="a" * 64,
        scope_hash="b" * 64,
        risk_summary="low",
    )

    plan.transition_to(ChangePlanStatus.FROZEN)
    plan.transition_to(ChangePlanStatus.SUPERSEDED)

    assert plan.status is ChangePlanStatus.SUPERSEDED


def test_frozen_plan_cannot_be_edited_back_to_draft() -> None:
    plan = ChangePlan(
        task_id="task-1",
        version=1,
        plan_hash="a" * 64,
        scope_hash="b" * 64,
        risk_summary="low",
    )
    plan.transition_to(ChangePlanStatus.FROZEN)

    with pytest.raises(DomainStateError):
        plan.transition_to(ChangePlanStatus.DRAFT)


def test_approval_must_be_pending_before_approval() -> None:
    approval = Approval(
        change_set_id="changeset-1",
        plan_hash="a" * 64,
        scope_hash="b" * 64,
        patch_hash="c" * 64,
        capability="r1",
        expires_at=datetime.now(UTC),
    )

    with pytest.raises(DomainStateError):
        approval.transition_to(ApprovalStatus.APPROVED)

    approval.transition_to(ApprovalStatus.PENDING)
    approval.transition_to(ApprovalStatus.APPROVED)
    approval.transition_to(ApprovalStatus.CONSUMED)
    assert approval.status is ApprovalStatus.CONSUMED


def test_consumed_approval_cannot_be_reused() -> None:
    approval = Approval(
        change_set_id="changeset-1",
        plan_hash="a" * 64,
        scope_hash="b" * 64,
        patch_hash="c" * 64,
        capability="r1",
        expires_at=datetime.now(UTC),
        status=ApprovalStatus.APPROVED,
    )
    approval.transition_to(ApprovalStatus.CONSUMED)

    with pytest.raises(DomainStateError):
        approval.transition_to(ApprovalStatus.APPROVED)


def test_changeset_requires_approval_before_applying() -> None:
    changeset = ChangeSet(task_id="task-1", plan_id="plan-1", patch_hash="a" * 64)

    changeset.transition_to(ChangeSetStatus.APPROVAL_PENDING)
    changeset.transition_to(ChangeSetStatus.APPROVED)
    changeset.transition_to(ChangeSetStatus.APPLYING)

    assert changeset.status is ChangeSetStatus.APPLYING


def test_changeset_can_record_verification_failure_and_revert() -> None:
    changeset = ChangeSet(
        task_id="task-1",
        plan_id="plan-1",
        patch_hash="a" * 64,
        status=ChangeSetStatus.APPLIED,
    )

    changeset.transition_to(ChangeSetStatus.VERIFYING)
    changeset.transition_to(ChangeSetStatus.VERIFICATION_FAILED)
    changeset.transition_to(ChangeSetStatus.REVERTED)

    assert changeset.status is ChangeSetStatus.REVERTED
