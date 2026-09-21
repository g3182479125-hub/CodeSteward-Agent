from pathlib import Path
from types import SimpleNamespace

import pytest

from devpilot import replay_workspace
from devpilot.replay_workspace import (
    ReplayabilityError,
    capture_git_snapshot,
    cleanup_private_worktree,
    create_private_worktree,
)


def test_non_git_project_is_not_replayable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        replay_workspace.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=128, stdout="", stderr="not git"),
    )

    snapshot = capture_git_snapshot(
        tmp_path,
        allowed_file_scope=["src/main.py"],
        verification_profile="python-syntax",
    )

    assert snapshot.replayable is False
    assert snapshot.start_commit_sha is None
    assert "git" in (snapshot.reason or "")


def test_dirty_git_project_is_not_replayable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = iter(
        [
            str(tmp_path),
            ".git",
            "a" * 40,
            " M src/main.py",
        ]
    )
    monkeypatch.setattr(
        replay_workspace.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=next(outputs), stderr=""),
    )

    snapshot = capture_git_snapshot(
        tmp_path,
        allowed_file_scope=["src/main.py"],
        verification_profile="python-syntax",
    )

    assert snapshot.replayable is False
    assert snapshot.start_commit_sha == "a" * 40
    assert snapshot.working_tree_clean is False
    assert snapshot.reason == "working tree is not clean"


def test_clean_snapshot_creates_and_cleans_private_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outputs = iter([str(tmp_path), ".git", "b" * 40, ""])
    calls: list[list[str]] = []

    def run(args, **kwargs):
        calls.append(list(args))
        if len(calls) <= 4:
            return SimpleNamespace(returncode=0, stdout=next(outputs), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(replay_workspace.subprocess, "run", run)
    snapshot = capture_git_snapshot(
        tmp_path,
        allowed_file_scope=["src/main.py", "tests/test_main.py"],
        verification_profile="python-run",
    )
    worktree = create_private_worktree(snapshot, private_root=tmp_path / "private")
    cleaned = cleanup_private_worktree(snapshot, worktree)

    assert snapshot.replayable is True
    assert worktree.start_commit_sha == "b" * 40
    assert cleaned.cleanup_error is None
    assert calls[4][3:6] == ["worktree", "add", "--detach"]
    assert calls[5][3:6] == ["worktree", "remove", "--force"]


def test_unsafe_scope_and_non_replayable_worktree_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ReplayabilityError, match="unsafe path"):
        capture_git_snapshot(
            tmp_path,
            allowed_file_scope=["../secrets.txt"],
            verification_profile="python-syntax",
        )
    snapshot = replay_workspace.GitReplaySnapshot(
        repository_root=str(tmp_path),
        repository_identity="",
        start_commit_sha=None,
        working_tree_clean=False,
        allowed_file_scope=(),
        verification_profile="python-syntax",
        replayable=False,
        reason="dirty",
    )
    with pytest.raises(ReplayabilityError, match="dirty"):
        create_private_worktree(snapshot, private_root=tmp_path / "private")
