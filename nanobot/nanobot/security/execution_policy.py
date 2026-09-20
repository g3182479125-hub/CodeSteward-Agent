"""Shared execution decisions and failure classification.

The agent has two deliberately separate questions:

* may this operation run under the current policy; and
* if it was allowed, why did it succeed or fail?

Keeping those questions separate prevents a normal command failure (for
example a missing executable) from being misreported as a security denial.
The values are plain strings so they can be forwarded to the model, WebUI,
logs, and future API responses without a translation layer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ExecutionDecision(StrEnum):
    """Decision made before an operation is executed."""

    AUTO_EXECUTE = "AUTO_EXECUTE"
    REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"
    BLOCKED_BY_POLICY = "BLOCKED_BY_POLICY"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"


class ExecutionErrorCode(StrEnum):
    """Stable machine-readable reason for a failed tool call."""

    POLICY_DENIED = "POLICY_DENIED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    TIMEOUT = "TIMEOUT"
    COMMAND_FAILED = "COMMAND_FAILED"
    OS_PERMISSION_DENIED = "OS_PERMISSION_DENIED"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CANCELLED = "CANCELLED"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


@dataclass(frozen=True, slots=True)
class FailureClassification:
    """Classification attached to a failed tool result."""

    code: ExecutionErrorCode
    decision: ExecutionDecision
    retryable: bool


def classify_exception(exc: BaseException) -> FailureClassification:
    """Classify an exception without guessing that every failure is policy.

    WorkspaceBoundaryError is imported lazily to keep this low-level module
    independent from the path implementation and to avoid import cycles.
    """

    from nanobot.security.workspace_policy import WorkspaceBoundaryError

    if isinstance(exc, WorkspaceBoundaryError):
        return FailureClassification(
            ExecutionErrorCode.POLICY_DENIED,
            ExecutionDecision.BLOCKED_BY_POLICY,
            False,
        )
    if isinstance(exc, PermissionError):
        return FailureClassification(
            ExecutionErrorCode.OS_PERMISSION_DENIED,
            ExecutionDecision.PRECONDITION_FAILED,
            False,
        )
    if isinstance(exc, (TimeoutError,)):
        return FailureClassification(
            ExecutionErrorCode.TIMEOUT,
            ExecutionDecision.PRECONDITION_FAILED,
            True,
        )
    if isinstance(exc, (FileNotFoundError, ModuleNotFoundError, ImportError)):
        return FailureClassification(
            ExecutionErrorCode.TOOL_UNAVAILABLE,
            ExecutionDecision.CAPABILITY_UNAVAILABLE,
            False,
        )
    if isinstance(exc, (ValueError, TypeError)):
        return FailureClassification(
            ExecutionErrorCode.INVALID_ARGUMENT,
            ExecutionDecision.PRECONDITION_FAILED,
            False,
        )
    if isinstance(exc, asyncio.CancelledError):
        return FailureClassification(
            ExecutionErrorCode.CANCELLED,
            ExecutionDecision.PRECONDITION_FAILED,
            True,
        )
    return FailureClassification(
        ExecutionErrorCode.UNKNOWN_ERROR,
        ExecutionDecision.PRECONDITION_FAILED,
        False,
    )

def error_result(content: str, exc: BaseException | None = None, **overrides: Any):
    """Create a metadata-rich ToolResult while keeping this module decoupled."""

    from nanobot.agent.tools.base import ToolResult

    classification = classify_exception(exc) if exc is not None else None
    return ToolResult.error(
        content,
        code=overrides.get("code", classification.code if classification else None),
        decision=overrides.get("decision", classification.decision if classification else None),
        retryable=overrides.get("retryable", classification.retryable if classification else None),
    )
