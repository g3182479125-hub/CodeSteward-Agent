"""Allowlisted verification profiles for an already-applied ChangeSet."""

from __future__ import annotations

import shlex
from enum import StrEnum

from .execution import ExecutionFile, ExecutionRequest
from .patches import PatchPlan


class VerificationProfile(StrEnum):
    """Commands controlled by DevPilot rather than supplied by the model."""

    PYTHON_SYNTAX = "python-syntax"
    NODE_SYNTAX = "node-syntax"


def build_verification_request(
    plan: PatchPlan, profile: VerificationProfile
) -> ExecutionRequest:
    """Build a bounded syntax-only request from persisted post-change contents."""

    suffixes = {
        VerificationProfile.PYTHON_SYNTAX: {".py"},
        VerificationProfile.NODE_SYNTAX: {".js", ".mjs", ".cjs"},
    }[profile]
    files = tuple(
        ExecutionFile(patch.relative_path, (patch.content or "").encode("utf-8"))
        for patch in plan.patches
        if patch.operation.value != "delete"
        and any(patch.relative_path.casefold().endswith(suffix) for suffix in suffixes)
    )
    if not files:
        raise ValueError(f"no files match verification profile {profile.value}")
    paths: list[str] = []
    for item in files:
        relative_path = item.relative_path.replace("\\", "/")
        paths.append(f"/workspace/devpilot/{relative_path}")
    if profile is VerificationProfile.PYTHON_SYNTAX:
        command = "python3 -m py_compile " + shlex.join(paths)
    else:
        command = "node --check " + shlex.join(paths)
    return ExecutionRequest(
        run_id=f"verify-{plan.change_set.id}",
        project_id=plan.project_id,
        files=files,
        command=command,
        timeout_seconds=120,
        network_allowed=False,
    )
