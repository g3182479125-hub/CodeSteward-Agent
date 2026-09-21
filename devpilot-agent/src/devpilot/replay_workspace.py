"""Trusted Git snapshots and private worktrees for Skill replay.

This module is deliberately independent from model and tool inputs.  It only
accepts a server-resolved project path and creates worktrees under a caller-
provided private temporary root.  A non-replayable snapshot is data, not an
exceptional reason to guess the old workspace state.
"""

from __future__ import annotations

import hashlib
import json
import os
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
    cleanup_record_path: Path | None = None


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
    identity = _repository_identity(root, git_dir)
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
    trusted_private_root: str | Path | None = None,
) -> ReplayWorktree:
    """Create a detached worktree under a server-owned private root."""

    if not snapshot.replayable or not snapshot.start_commit_sha:
        raise ReplayabilityError(snapshot.reason or "snapshot is not replayable")
    root = _validate_private_root(
        snapshot,
        private_root=private_root,
        trusted_private_root=trusted_private_root,
    )
    try:
        current_identity = _repository_identity(
            Path(_git(Path(snapshot.repository_root), "rev-parse", "--show-toplevel")),
            _git(Path(snapshot.repository_root), "rev-parse", "--git-dir"),
        )
    except ReplayabilityError as exc:
        raise ReplayabilityError("source repository identity validation failed") from exc
    if current_identity != snapshot.repository_identity:
        raise ReplayabilityError("source repository identity changed")
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
    try:
        actual_root = Path(_git(path, "rev-parse", "--show-toplevel")).resolve()
        actual_commit = _git(path, "rev-parse", "HEAD")
    except ReplayabilityError as exc:
        _remove_created_worktree(snapshot, path)
        raise ReplayabilityError("private worktree validation failed") from exc
    if actual_root != path.resolve() or actual_commit != snapshot.start_commit_sha:
        _remove_created_worktree(snapshot, path)
        raise ReplayabilityError("private worktree validation failed")
    return ReplayWorktree(path=path, start_commit_sha=snapshot.start_commit_sha)


def cleanup_private_worktree(
    snapshot: GitReplaySnapshot,
    worktree: ReplayWorktree,
    *,
    trusted_private_root: str | Path | None = None,
) -> ReplayWorktree:
    """Remove one replay worktree and retain cleanup failure as visible state."""

    root: Path | None = None
    path = worktree.path
    try:
        root = _validate_private_root(
            snapshot,
            private_root=worktree.path.parent,
            trusted_private_root=trusted_private_root,
        )
        path = worktree.path.resolve()
        if path.parent != root or not path.name.startswith("replay-"):
            raise ReplayabilityError("private worktree path is outside the trusted root")
        result = subprocess.run(
            [
                "git",
                "-C",
                snapshot.repository_root,
                "worktree",
                "remove",
                "--force",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, ReplayabilityError, subprocess.TimeoutExpired) as exc:
        error = str(exc)
        record_path = _persist_cleanup_failure(root, path, error)
        return ReplayWorktree(worktree.path, worktree.start_commit_sha, error, record_path)
    if result.returncode != 0:
        return ReplayWorktree(
            worktree.path,
            worktree.start_commit_sha,
            result.stderr.strip() or "private worktree cleanup failed",
            _persist_cleanup_failure(
                root,
                path,
                result.stderr.strip() or "private worktree cleanup failed",
            ),
        )
    return ReplayWorktree(worktree.path, worktree.start_commit_sha)


def _validate_private_root(
    snapshot: GitReplaySnapshot,
    *,
    private_root: str | Path,
    trusted_private_root: str | Path | None,
) -> Path:
    """Resolve a configured private root and reject repository-contained paths."""

    configured = trusted_private_root or os.environ.get("DEVPILOT_REPLAY_PRIVATE_ROOT")
    if configured is None:
        raise ReplayabilityError("trusted private replay root is not configured")
    unresolved_root = Path(private_root).expanduser()
    unresolved_trusted = Path(configured).expanduser()
    root_chain = (*unresolved_root.parents, unresolved_root)
    trusted_chain = (*unresolved_trusted.parents, unresolved_trusted)
    if any(_is_link_like(item) for item in (*root_chain, *trusted_chain)):
        raise ReplayabilityError("private replay root cannot be a symlink")
    root = unresolved_root.resolve()
    trusted = unresolved_trusted.resolve()
    if root != trusted:
        raise ReplayabilityError("private replay root is not trusted")
    repository = Path(snapshot.repository_root).expanduser().resolve()
    if root == repository or repository in root.parents:
        raise ReplayabilityError("private replay root is inside the repository")
    try:
        if root.exists() and root.is_symlink():
            raise ReplayabilityError("private replay root cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True)
    except (OSError, ReplayabilityError) as exc:
        if isinstance(exc, ReplayabilityError):
            raise
        raise ReplayabilityError("private replay root is unavailable") from exc
    if not root.is_dir():
        raise ReplayabilityError("private replay root is not a directory")
    return root


def _is_link_like(path: Path) -> bool:
    """Detect symlinks and Windows directory junctions before resolving them."""

    if os.path.islink(str(path)):
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _repository_identity(root: Path, git_dir: str) -> str:
    return hashlib.sha256(f"{root.resolve()}\0{git_dir}".encode()).hexdigest()


def _remove_created_worktree(snapshot: GitReplaySnapshot, path: Path) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            snapshot.repository_root,
            "worktree",
            "remove",
            "--force",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _persist_cleanup_failure(
    root: Path | None,
    path: Path,
    error: str,
) -> Path | None:
    if root is None:
        return None
    try:
        pending = root / "cleanup-pending"
        pending.mkdir(parents=True, exist_ok=True)
        record = pending / f"{uuid.uuid4().hex}.json"
        record.write_text(
            json.dumps(
                {"path": str(path), "error": error},
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return record
    except OSError:
        return None
