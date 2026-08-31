"""Small framework-free entities used by the P2A-01 state model."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .enums import ApprovalStatus, ChangePlanStatus, ChangeSetStatus, FileOperation, TaskStatus
from .state_machine import (
    APPROVAL_TRANSITIONS,
    CHANGE_PLAN_TRANSITIONS,
    CHANGE_SET_TRANSITIONS,
    TASK_TRANSITIONS,
    transition,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Task:
    project_id: str
    request_text: str
    kind: str = "feature"
    id: str = field(default_factory=_uuid)
    status: TaskStatus = TaskStatus.DRAFT
    scope: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=_now)

    def transition_to(self, target: TaskStatus) -> None:
        self.status = transition("Task", self.status, target, TASK_TRANSITIONS)


@dataclass
class ChangePlan:
    task_id: str
    version: int
    plan_hash: str
    scope_hash: str
    risk_summary: str
    check_ids: tuple[str, ...] = ()
    stop_conditions: tuple[str, ...] = ()
    id: str = field(default_factory=_uuid)
    status: ChangePlanStatus = ChangePlanStatus.DRAFT
    created_at: datetime = field(default_factory=_now)

    def transition_to(self, target: ChangePlanStatus) -> None:
        self.status = transition("ChangePlan", self.status, target, CHANGE_PLAN_TRANSITIONS)


@dataclass
class FileChange:
    relative_path: str
    operation: FileOperation
    content_hash: str
    expected_sha256: str | None = None
    before_size: int = 0
    after_sha256: str | None = None


@dataclass
class ChangeSet:
    task_id: str
    plan_id: str
    patch_hash: str
    id: str = field(default_factory=_uuid)
    status: ChangeSetStatus = ChangeSetStatus.PLANNED
    file_changes: tuple[FileChange, ...] = ()
    before_manifest: tuple[str, ...] = ()
    after_manifest: tuple[str, ...] = ()
    rollback_manifest: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=_now)

    def transition_to(self, target: ChangeSetStatus) -> None:
        self.status = transition("ChangeSet", self.status, target, CHANGE_SET_TRANSITIONS)


@dataclass
class Approval:
    change_set_id: str
    plan_hash: str
    scope_hash: str
    patch_hash: str
    capability: str
    expires_at: datetime
    id: str = field(default_factory=_uuid)
    status: ApprovalStatus = ApprovalStatus.DRAFT
    token_digest: str | None = None
    created_at: datetime = field(default_factory=_now)
    consumed_at: datetime | None = None

    def transition_to(self, target: ApprovalStatus) -> None:
        self.status = transition("Approval", self.status, target, APPROVAL_TRANSITIONS)
