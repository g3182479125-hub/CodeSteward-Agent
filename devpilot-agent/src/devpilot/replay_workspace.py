"""Trusted Git snapshots and private worktrees for Skill replay.

This module is deliberately independent from model and tool inputs.  It only
accepts a server-resolved project path and creates worktrees under a caller-
provided private temporary root.  A non-replayable snapshot is data, not an
exceptional reason to guess the old workspace state.
"""

from __future__ import annotations

import hashlib
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path


class ReplayabilityError(ValueError):
    """Raised when a snapshot cannot safely produce an isolated worktree."""


@dataclass(frozen=True, slots=True)
class GitReplaySnapshot:
    repository_root: str
    repository_identity: str
    start_commit_sha: str | None
    working_tree_clean: bool
    allowed_file_scope: tuple[str, ...]
    verification_profile: str
    replayable: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ReplayWorktree:
    path: Path
    start_commit_sha: str
    cleanup_error: str | None = None


def _git(project: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=project,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReplayabilityError("git command is unavailable or timed out") from exc
    if result.returncode != 0:
        raise ReplayabilityError("git repository metadata is unavailable")
    return result.stdout.strip()


def _scope(scope: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    values: list[str] = []
    for item in scope:
        normalized = str(item).replace("\\", "/").strip("/")
        parts = tuple(part for part in normalized.split("/") if part)
        if not parts or any(part in {".", ".."} or ":" in part for part in parts):
            raise ReplayabilityError("allowed file scope contains an unsafe path")
        values.append("/".join(parts))
    if len(set(values)) != len(values):
        raise ReplayabilityError("allowed file scope contains duplicates")
    return tuple(values)


def capture_git_snapshot(
    project_path: str | Path,
    *,
    allowed_file_scope: tuple[str, ...] | list[str],
    verification_profile: str,
) -> GitReplaySnapshot:
    """Capture immutable Git facts without changing the real project."""

    project = Path(project_path).expanduser().resolve()
    scope = _scope(allowed_file_scope)
    profile = str(verification_profile).strip()
    if not profile:
        raise ReplayabilityError("verification profile is required")
    try:
        root = Path(_git(project, "rev-parse", "--show-toplevel")).resolve()
        git_dir = _git(project, "rev-parse", "--git-dir")
        commit = _git(project, "rev-parse", "HEAD")
        status = _git(project, "status", "--porcelain=v1", "--untracked-files=all")
    except ReplayabilityError as exc:
        return GitReplaySnapshot(
            repository_root=str(project),
            repository_identity="",
            start_commit_sha=None,
            working_tree_clean=False,
            allowed_file_scope=scope,
            verification_profile=profile,
            replayable=False,
            reason=str(exc),
        )
    identity = hashlib.sha256(f"{root}\0{git_dir}".encode()).hexdigest()
    if not commit or len(commit) < 7:
        return GitReplaySnapshot(
            repository_root=str(root),
            repository_identity=identity,
            start_commit_sha=None,
            working_tree_clean=not status,
            allowed_file_scope=scope,
            verification_profile=profile,
            replayable=False,
            reason="repository has no resolvable start commit",
        )
    if status:
        return GitReplaySnapshot(
            repository_root=str(root),
            repository_identity=identity,
            start_commit_sha=commit,
            working_tree_clean=False,
            allowed_file_scope=scope,
            verification_profile=profile,
            replayable=False,
            reason="working tree is not clean",
        )
    return GitReplaySnapshot(
        repository_root=str(root),
        repository_identity=identity,
        start_commit_sha=commit,
        working_tree_clean=True,
        allowed_file_scope=scope,
        verification_profile=profile,
        replayable=True,
    )


def create_private_worktree(
    snapshot: GitReplaySnapshot,
    *,
    private_root: str | Path,
) -> ReplayWorktree:
    """Create a detached worktree under a server-owned private root."""

    if not snapshot.replayable or not snapshot.start_commit_sha:
        raise ReplayabilityError(snapshot.reason or "snapshot is not replayable")
    root = Path(private_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"replay-{uuid.uuid4().hex}"
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                snapshot.repository_root,
                "worktree",
                "add",
                "--detach",
                str(path),
                snapshot.start_commit_sha,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReplayabilityError("private worktree creation failed") from exc
    if result.returncode != 0:
        raise ReplayabilityError("private worktree creation failed")
    return ReplayWorktree(path=path, start_commit_sha=snapshot.start_commit_sha)


def cleanup_private_worktree(
    snapshot: GitReplaySnapshot,
    worktree: ReplayWorktree,
) -> ReplayWorktree:
    """Remove one replay worktree and retain cleanup failure as visible state."""

    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                snapshot.repository_root,
                "worktree",
                "remove",
                "--force",
                str(worktree.path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ReplayWorktree(worktree.path, worktree.start_commit_sha, str(exc))
    if result.returncode != 0:
        return ReplayWorktree(
            worktree.path,
            worktree.start_commit_sha,
            result.stderr.strip() or "private worktree cleanup failed",
        )
    return ReplayWorktree(worktree.path, worktree.start_commit_sha)
