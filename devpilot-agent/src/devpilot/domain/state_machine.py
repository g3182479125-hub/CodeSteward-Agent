"""Deterministic state transition tables for the DevPilot domain."""

from __future__ import annotations

from enum import StrEnum
from typing import TypeVar

from .enums import ApprovalStatus, ChangePlanStatus, ChangeSetStatus, TaskStatus
from .errors import DomainStateError

StatusT = TypeVar("StatusT", bound=StrEnum)


TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.DRAFT: frozenset({TaskStatus.PLANNING, TaskStatus.CANCELLED}),
    TaskStatus.PLANNING: frozenset(
        {TaskStatus.WAITING_APPROVAL, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.WAITING_APPROVAL: frozenset(
        {TaskStatus.APPLYING, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.APPLYING: frozenset({TaskStatus.VERIFYING, TaskStatus.FAILED, TaskStatus.CANCELLED}),
    TaskStatus.VERIFYING: frozenset(
        {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}

CHANGE_PLAN_TRANSITIONS: dict[ChangePlanStatus, frozenset[ChangePlanStatus]] = {
    ChangePlanStatus.DRAFT: frozenset({ChangePlanStatus.FROZEN, ChangePlanStatus.EXPIRED}),
    ChangePlanStatus.FROZEN: frozenset({ChangePlanStatus.SUPERSEDED, ChangePlanStatus.EXPIRED}),
    ChangePlanStatus.SUPERSEDED: frozenset(),
    ChangePlanStatus.EXPIRED: frozenset(),
}

APPROVAL_TRANSITIONS: dict[ApprovalStatus, frozenset[ApprovalStatus]] = {
    ApprovalStatus.DRAFT: frozenset({ApprovalStatus.PENDING, ApprovalStatus.REJECTED}),
    ApprovalStatus.PENDING: frozenset(
        {
            ApprovalStatus.APPROVED,
            ApprovalStatus.EXPIRED,
            ApprovalStatus.REVOKED,
            ApprovalStatus.REJECTED,
        }
    ),
    ApprovalStatus.APPROVED: frozenset(
        {ApprovalStatus.CONSUMED, ApprovalStatus.EXPIRED, ApprovalStatus.REVOKED}
    ),
    ApprovalStatus.CONSUMED: frozenset(),
    ApprovalStatus.EXPIRED: frozenset(),
    ApprovalStatus.REVOKED: frozenset(),
    ApprovalStatus.REJECTED: frozenset(),
}

CHANGE_SET_TRANSITIONS: dict[ChangeSetStatus, frozenset[ChangeSetStatus]] = {
    ChangeSetStatus.PLANNED: frozenset(
        {ChangeSetStatus.APPROVAL_PENDING, ChangeSetStatus.REJECTED, ChangeSetStatus.EXPIRED}
    ),
    ChangeSetStatus.APPROVAL_PENDING: frozenset(
        {ChangeSetStatus.APPROVED, ChangeSetStatus.REJECTED, ChangeSetStatus.EXPIRED}
    ),
    ChangeSetStatus.APPROVED: frozenset(
        {ChangeSetStatus.APPLYING, ChangeSetStatus.REJECTED, ChangeSetStatus.EXPIRED}
    ),
    ChangeSetStatus.APPLYING: frozenset({ChangeSetStatus.APPLIED, ChangeSetStatus.APPLY_FAILED}),
    ChangeSetStatus.APPLIED: frozenset({ChangeSetStatus.VERIFYING, ChangeSetStatus.REVERTED}),
    ChangeSetStatus.VERIFYING: frozenset(
        {ChangeSetStatus.VERIFIED, ChangeSetStatus.VERIFICATION_FAILED}
    ),
    ChangeSetStatus.VERIFIED: frozenset({ChangeSetStatus.ACCEPTED, ChangeSetStatus.REVERTED}),
    ChangeSetStatus.VERIFICATION_FAILED: frozenset(
        {ChangeSetStatus.ACCEPTED, ChangeSetStatus.REVERTED}
    ),
    ChangeSetStatus.ACCEPTED: frozenset(),
    ChangeSetStatus.REVERTED: frozenset(),
    ChangeSetStatus.REJECTED: frozenset(),
    ChangeSetStatus.EXPIRED: frozenset(),
    ChangeSetStatus.APPLY_FAILED: frozenset(),
}


def transition(
    entity: str, current: StatusT, target: StatusT, allowed: dict[StatusT, frozenset[StatusT]]
) -> StatusT:
    """Return ``target`` only when the transition is explicitly allowed."""
    if target not in allowed.get(current, frozenset()):
        raise DomainStateError(entity, current.value, target.value)
    return target
