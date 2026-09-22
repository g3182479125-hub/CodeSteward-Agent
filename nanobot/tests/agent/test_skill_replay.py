from __future__ import annotations

import subprocess
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from nanobot.agent.skill_replay import (
    ReplayRequest,
    ReplayTrialResult,
    ReplayWorktreeLease,
    grade_replay,
    run_skill_replay,
)
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.utils.llm_runtime import LLMRuntime


class _FakeProvider(LLMProvider):
    def __init__(self, response: str) -> None:
        super().__init__()
        self.response = response

    def get_default_model(self) -> str:
        return "fake/model"

    async def chat(self, **_: Any) -> LLMResponse:
        return LLMResponse(content=self.response, tool_calls=[], usage={"total_tokens": 3})


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "skills" / "repo-analysis").mkdir(parents=True)
    (repo / "skills" / "repo-analysis" / "SKILL.md").write_text(
        "---\nname: repo-analysis\n---\nread only\n", encoding="utf-8"
    )
    _git(repo, "init")
    _git(repo, "config", "user.email", "replay@example.invalid")
    _git(repo, "config", "user.name", "Replay Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo


def _request(
    repo: Path,
    private_root: Path,
    response: str,
    paths: list[Path],
) -> ReplayRequest:
    private_root.mkdir(parents=True, exist_ok=True)

    def make_worktree() -> ReplayWorktreeLease:
        path = private_root / f"replay-{uuid.uuid4().hex}"
        _git(repo, "worktree", "add", "--detach", str(path), "HEAD")
        paths.append(path)

        def cleanup() -> None:
            _git(repo, "worktree", "remove", "--force", str(path))

        return ReplayWorktreeLease(path=path, cleanup=cleanup)

    provider = _FakeProvider(response)
    return ReplayRequest(
        profile_name="repo-analysis",
        skill_id="repo-analysis",
        version="candidate",
        skill_relative_path="skills/repo-analysis/SKILL.md",
        snapshot_ref="snapshot-fixture",
        worktree_factory=make_worktree,
        initial_messages=({"role": "user", "content": "分析项目并指出测试入口。"},),
        tools=ToolRegistry(),
        runtime=LLMRuntime.capture(provider, "fake/model", context_window_tokens=4096),
        allowed_tools=frozenset(),
        task_id="task-replay",
        project_id="project-replay",
        session_key=f"replay-{uuid.uuid4()}",
        model_id="fake/model",
    )


@pytest.mark.asyncio
async def test_replay_uses_agent_runner_and_cleans_each_distinct_worktree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    private_root = tmp_path / "private-replays"
    paths: list[Path] = []
    request = _request(repo, private_root, "测试入口是 tests/test_api.py。", paths)

    first = await run_skill_replay(request, "---\nname: repo-analysis\n---\n候选 Skill\n")
    second = await run_skill_replay(request, "---\nname: repo-analysis\n---\n候选 Skill 2\n")

    assert first.status == "passed"
    assert second.status == "passed"
    assert first.final_output == "测试入口是 tests/test_api.py。"
    assert first.worktree_path != second.worktree_path
    assert first.changed_paths == ("skills/repo-analysis/SKILL.md",)
    assert second.changed_paths == ("skills/repo-analysis/SKILL.md",)
    assert all(not path.exists() for path in paths)
    grade = grade_replay(request, first, expected_behavior="tests/test_api.py")
    assert grade.grader_kind == "analysis_fact"
    assert grade.passed is True


@pytest.mark.asyncio
async def test_replay_grader_rejects_infrastructure_and_untrusted_verification(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    request = _request(repo, tmp_path / "private-replays", "修复完成", [])
    result = await run_skill_replay(request, "---\nname: repo-analysis\n---\n候选\n")

    bug_request = replace(request, profile_name="bug-fix", skill_id="bug-fix")
    assert grade_replay(
        bug_request,
        result,
        trusted_verification_passed=False,
        original_failure_repaired=True,
        allowed_file_scope=("src/main.py",),
    ).passed is False


def test_replay_has_three_trusted_graders_and_rejects_scope_or_timeout() -> None:
    request = ReplayRequest(
        profile_name="feature-delivery",
        skill_id="feature-delivery",
        version="candidate",
        skill_relative_path="skills/feature-delivery/SKILL.md",
        snapshot_ref="snapshot-fixture",
        worktree_factory=lambda: ReplayWorktreeLease(Path("."), lambda: None),
        initial_messages=(),
        tools=ToolRegistry(),
        runtime=LLMRuntime.capture(_FakeProvider("ok"), "fake/model", context_window_tokens=4096),
        allowed_tools=frozenset(),
        task_id="task-replay",
        project_id="project-replay",
        session_key="replay-session",
        model_id="fake/model",
    )
    passed = ReplayTrialResult(
        status="passed",
        final_output="ok",
        tool_events=(),
        tools_used=(),
        usage={"total_tokens": 3},
        duration_seconds=0.1,
        changed_paths=("src/main.py",),
        worktree_path="private/replay",
    )
    delivery = grade_replay(
        request,
        passed,
        trusted_verification_passed=True,
        allowed_file_scope=("src/main.py",),
    )
    assert delivery.grader_kind == "delivery_verification"
    assert delivery.passed is True

    bug = grade_replay(
        replace(request, profile_name="bug-fix", skill_id="bug-fix"),
        passed,
        trusted_verification_passed=True,
        original_failure_repaired=True,
        allowed_file_scope=("src/main.py",),
    )
    assert bug.grader_kind == "bug_fix_verification"
    assert bug.passed is True

    outside_scope = grade_replay(
        request,
        replace(passed, changed_paths=("src/main.py", "secrets.txt")),
        trusted_verification_passed=True,
        allowed_file_scope=("src/main.py",),
    )
    assert outside_scope.passed is False
    assert outside_scope.changed_paths == ("secrets.txt",)

    timed_out = grade_replay(
        request,
        replace(passed, status="timeout"),
        trusted_verification_passed=True,
        allowed_file_scope=("src/main.py",),
    )
    assert timed_out.passed is False
