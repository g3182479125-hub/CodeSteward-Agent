"""Domain state and operation vocabularies for P2A-01."""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    DRAFT = "draft"
    PLANNING = "planning"
    WAITING_APPROVAL = "waiting_approval"
    APPLYING = "applying"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChangePlanStatus(StrEnum):
    DRAFT = "draft"
    FROZEN = "frozen"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"


class ApprovalStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"
    REJECTED = "rejected"


class ChangeSetStatus(StrEnum):
    PLANNED = "planned"
    APPROVAL_PENDING = "approval_pending"
    APPROVED = "approved"
    APPLYING = "applying"
    APPLIED = "applied"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"
    DELIVERY_FAILED = "delivery_failed"
    # Legacy persisted value. New deliveries finish at VERIFIED or
    # VERIFICATION_FAILED; this value remains readable for old databases only.
    ACCEPTED = "accepted"
    REVERTED = "reverted"
    REJECTED = "rejected"
    EXPIRED = "expired"
    APPLY_FAILED = "apply_failed"


class FileOperation(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
