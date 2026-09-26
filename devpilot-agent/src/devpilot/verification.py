"""Allowlisted verification profiles for an explicitly snapshotted ChangeSet."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
from enum import StrEnum
from pathlib import Path

from .execution import (
    MAX_EXECUTION_BUNDLE_BYTES,
    MAX_EXECUTION_FILE_BYTES,
    MAX_EXECUTION_FILES,
    ExecutionFile,
    ExecutionRequest,
)
from .patches import PatchPlan
from .sensitive_paths import is_public_password_template

MAX_SNAPSHOT_FILES = MAX_EXECUTION_FILES
MAX_SNAPSHOT_BYTES = MAX_EXECUTION_BUNDLE_BYTES
VERIFICATION_PROFILE_VERSION = "1"
PYTHON_REGRESSION_DEPENDENCY_TEMPLATE = "python-stdlib-unittest-v1"
PYTHON_REGRESSION_TIMEOUT_SECONDS = 90
_FORBIDDEN_PARTS = frozenset(
    {".git", ".hg", ".svn", "node_modules", "dist", "build", ".venv", "__pycache__"}
)


class VerificationProfile(StrEnum):
    """Commands controlled by DevPilot rather than supplied by the model."""

    PYTHON_SYNTAX = "python-syntax"
    PYTHON_REGRESSION = "python-regression"
    PYTHON_RUN = "python-run"
    NODE_SYNTAX = "node-syntax"
    REACT_STATIC = "react-static"


def _is_secret_name(name: str) -> bool:
    lowered = name.casefold()
    if lowered.startswith(".env"):
        return True
    if any(
        word in lowered
        for word in (
            "secret",
            "credential",
            "password",
            "api_key",
            "api-key",
            "apikey",
            "access_token",
            "access-token",
            "auth_token",
            "auth-token",
        )
    ):
        return True
    return lowered.endswith((".pem", ".key", ".crt", ".cer", ".p12", ".pfx", ".jks"))


def is_verification_snapshot_excluded(relative_path: str) -> bool:
    """Return whether a path must stay out of a sandbox project snapshot."""

    parts = tuple(part for part in relative_path.replace("\\", "/").split("/") if part)
    return any(
        part.casefold() in _FORBIDDEN_PARTS
        or (_is_secret_name(part) and not is_public_password_template(parts, index))
        for index, part in enumerate(parts)
    )


def read_verification_snapshot_file(path: Path, relative_path: str) -> bytes | None:
    """Read one bounded, non-secret text file for a sandbox snapshot."""

    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"unable to read verification snapshot file: {relative_path}") from exc
    if b"\x00" in content:
        return None
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(content) > MAX_EXECUTION_FILE_BYTES:
        raise ValueError(f"verification snapshot file exceeds 512 KiB: {relative_path}")
    return content


def _pytest_working_directory(files: tuple[ExecutionFile, ...]) -> str:
    """Choose one server-owned nested project root for pytest execution.

    A repository may keep its runnable project below a wrapper directory, as
    in ``seed_repo/tests``.  Running from that nested root preserves the same
    import semantics as the project's own local test command while keeping
    the command itself fixed and model-independent.
    """

    candidates: set[str] = set()
    for item in files:
        parts = tuple(part for part in item.relative_path.replace("\\", "/").split("/") if part)
        if not parts:
            continue
        if parts[-1].startswith("test_") or parts[-1].endswith("_test.py"):
            if "tests" in parts[:-1]:
                index = parts.index("tests")
                candidates.add("/".join(parts[:index]))
            elif len(parts) > 1:
                candidates.add("/".join(parts[:-1]))
            else:
                candidates.add("")
    if len(candidates) == 1:
        return next(iter(candidates))
    return ""


def _project_snapshot(plan: PatchPlan) -> tuple[ExecutionFile, ...]:
    """Collect a bounded UTF-8 snapshot, overlaying the proposed patch contents."""

    project = plan.project_path.resolve()
    if not project.is_dir():
        raise ValueError("verification project directory does not exist")

    files: dict[str, bytes] = {}
    try:
        for root, directories, names in os.walk(project, topdown=True, followlinks=False):
            directories[:] = sorted(
                name
                for name in directories
                if not is_verification_snapshot_excluded(
                    str(Path(root, name).relative_to(project))
                )
                and not Path(root, name).is_symlink()
            )
            for name in sorted(names):
                path = Path(root, name)
                if path.is_symlink():
                    continue
                relative_path = path.relative_to(project).as_posix()
                if is_verification_snapshot_excluded(relative_path):
                    continue
                content = read_verification_snapshot_file(path, relative_path)
                if content is not None:
                    files[relative_path] = content
    except OSError as exc:
        raise ValueError("unable to enumerate verification project snapshot") from exc

    for patch in plan.patches:
        relative_path = patch.relative_path.replace("\\", "/")
        if patch.operation.value == "delete":
            files.pop(relative_path, None)
        else:
            if patch.content is None:
                raise ValueError(f"patch has no content for {relative_path}")
            content = patch.content.encode("utf-8")
            if len(content) > MAX_EXECUTION_FILE_BYTES:
                raise ValueError(f"verification snapshot file exceeds 512 KiB: {relative_path}")
            files[relative_path] = content

    ordered = tuple(ExecutionFile(path, files[path]) for path in sorted(files))
    if not ordered:
        raise ValueError("verification snapshot is empty")
    if len(ordered) > MAX_SNAPSHOT_FILES:
        raise ValueError(f"verification snapshot exceeds {MAX_SNAPSHOT_FILES} files")
    total_bytes = sum(len(item.content) for item in ordered)
    if total_bytes > MAX_SNAPSHOT_BYTES:
        raise ValueError("verification snapshot exceeds the 8 MiB limit")
    return ordered


def build_verification_request(plan: PatchPlan, profile: VerificationProfile) -> ExecutionRequest:
    """Build a bounded request from a complete post-change project snapshot.

    The command is selected by this server-side allowlist. A model or browser
    client can choose a profile, but it cannot provide an arbitrary command.
    """

    suffixes = {
        VerificationProfile.PYTHON_SYNTAX: {".py"},
        VerificationProfile.PYTHON_REGRESSION: {".py"},
        VerificationProfile.PYTHON_RUN: {".py"},
        VerificationProfile.NODE_SYNTAX: {".js", ".mjs", ".cjs"},
        # G-02 is a static React fixture.  Its contract is intentionally
        # represented as an allowlisted profile; before P6 only FakeExecutionBackend
        # may consume it, so no local React/npm process is started here.
        VerificationProfile.REACT_STATIC: {".jsx"},
    }[profile]
    changed_files = tuple(
        patch.relative_path.replace("\\", "/")
        for patch in plan.patches
        if patch.operation.value != "delete"
        and any(patch.relative_path.casefold().endswith(suffix) for suffix in suffixes)
    )
    if not changed_files:
        raise ValueError(f"no files match verification profile {profile.value}")

    files = _project_snapshot(plan)
    paths = [f"/workspace/devpilot/{relative_path}" for relative_path in changed_files]
    if profile is VerificationProfile.PYTHON_SYNTAX:
        command = "python3 -m py_compile " + shlex.join(paths)
    elif profile is VerificationProfile.PYTHON_REGRESSION:
        normalized_snapshot_paths = tuple(item.relative_path.replace("\\", "/") for item in files)
        if not any(
            "/tests/" in f"/{relative_path}"
            or relative_path.startswith("tests/")
            for relative_path in normalized_snapshot_paths
        ):
            raise ValueError("python-regression requires a tests/ snapshot")
        command = "python -m pytest -q"
    elif profile is VerificationProfile.PYTHON_RUN:
        if len(paths) != 1:
            raise ValueError("python-run requires exactly one changed Python file")
        command = "python3 " + shlex.join(paths)
    elif profile is VerificationProfile.NODE_SYNTAX:
        command = "node --check " + shlex.join(paths)
    else:
        command = "react-static " + shlex.join(paths)
    return ExecutionRequest(
        run_id=f"verify-{plan.change_set.id}",
        project_id=plan.project_id,
        files=files,
        command=command,
        timeout_seconds=(
            PYTHON_REGRESSION_TIMEOUT_SECONDS
            if profile is VerificationProfile.PYTHON_REGRESSION
            else 120
        ),
        network_allowed=False,
        command_profile=profile.value,
        working_directory=(
            _pytest_working_directory(files)
            if profile is VerificationProfile.PYTHON_REGRESSION
            else ""
        ),
    )


def verification_binding(plan: PatchPlan, profile: VerificationProfile) -> dict[str, str]:
    """Return stable, secret-free identities for one server-owned verification request."""
    request = build_verification_request(plan, profile)
    snapshot = [
        {"path": item.relative_path, "sha256": hashlib.sha256(item.content).hexdigest()}
        for item in request.files
    ]
    snapshot_payload = json.dumps(
        {
            "profile": profile.value,
            "profile_version": VERIFICATION_PROFILE_VERSION,
            "command": request.command,
            "files": snapshot,
            "timeout_seconds": request.timeout_seconds,
            "network_allowed": request.network_allowed,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    check_payload = json.dumps(
        {"profile": profile.value, "profile_version": VERIFICATION_PROFILE_VERSION},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "verification_profile": profile.value,
        "verification_profile_version": VERIFICATION_PROFILE_VERSION,
        "verification_snapshot_hash": hashlib.sha256(snapshot_payload).hexdigest(),
        "verification_check_set_hash": hashlib.sha256(check_payload).hexdigest(),
    }
