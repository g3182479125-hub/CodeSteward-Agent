import sqlite3
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from nanobot.agent.skill_replay import ReplayRequest, ReplayWorktreeLease
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.utils.llm_runtime import LLMRuntime

from devpilot.paths import allowed_root_id, repository_fingerprint
from devpilot.replay_workspace import (
    GitReplaySnapshot,
    cleanup_private_worktree,
    create_private_worktree,
)
from devpilot.skill_improvement import (
    IMPROVEMENT_CASE_STATES,
    REPLAY_TRIAL_STATUSES,
    SKILL_IMPROVEMENT_TARGETS,
    SkillImprovementError,
    SkillImprovementStore,
    content_sha256,
    task_snapshot_ref,
)


class _SI08ReadCodeTool(Tool):
    def __init__(self, evidence_ref: str) -> None:
        self.evidence_ref = evidence_ref

    @property
    def name(self) -> str:
        return "devpilot_read_code"

    @property
    def description(self) -> str:
        return "Read the trusted synthetic test entry point."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, *, path: str) -> str:
        assert path == "tests/test_api.py"
        return f"tests/test_api.py {self.evidence_ref}"


class _SI08Provider(LLMProvider):
    def __init__(self, *, candidate: bool) -> None:
        super().__init__()
        self.candidate = candidate
        self.calls = 0

    def get_default_model(self) -> str:
        return "synthetic/si08"

    async def chat(self, **_: Any) -> LLMResponse:
        self.calls += 1
        if not self.candidate:
            return LLMResponse(
                content="无法定位测试入口。",
                usage={"total_tokens": 3},
            )
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="si08-read-1",
                        name="devpilot_read_code",
                        arguments={"path": "tests/test_api.py"},
                    )
                ],
                finish_reason="tool_calls",
                usage={"total_tokens": 3},
            )
        return LLMResponse(
            content="输出 tests/test_api.py。",
            usage={"total_tokens": 3},
        )


def _git_fixture(workspace: Path) -> None:
    (workspace / "tests").mkdir()
    (workspace / "tests" / "test_api.py").write_text(
        "def test_api_entrypoint():\n    assert True\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(workspace), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.email", "si08@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.name", "SI08 Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "commit", "-qm", "synthetic SI-08 fixture"],
        check=True,
    )


def _si08_request(
    *,
    snapshot: GitReplaySnapshot,
    snapshot_ref: str,
    private_root: Path,
    provider: LLMProvider,
    version: str,
    evidence_ref: str,
) -> ReplayRequest:
    private_root.mkdir(parents=True, exist_ok=True)

    def worktree_factory() -> ReplayWorktreeLease:
        created = create_private_worktree(
            snapshot,
            private_root=private_root,
            trusted_private_root=private_root,
        )

        def cleanup() -> None:
            cleanup_private_worktree(
                snapshot,
                created,
                trusted_private_root=private_root,
            )

        return ReplayWorktreeLease(path=created.path, cleanup=cleanup)

    tools = ToolRegistry()
    tools.register(_SI08ReadCodeTool(evidence_ref))
    return ReplayRequest(
        profile_name="repo-analysis",
        skill_id="repo-analysis",
        version=version,
        skill_relative_path="skills/repo-analysis/SKILL.md",
        snapshot_ref=snapshot_ref,
        worktree_factory=worktree_factory,
        initial_messages=(
            {"role": "user", "content": "请找出项目里的测试入口。"},
        ),
        tools=tools,
        runtime=LLMRuntime.capture(
            provider,
            "synthetic/si08",
            context_window_tokens=4096,
        ),
        allowed_tools=frozenset({"devpilot_read_code"}),
        task_id="task-si08-real-runner",
        project_id="project-si08",
        session_key=f"si08-{version}-{uuid.uuid4()}",
        model_id="synthetic/si08",
        private_worktree_root=private_root,
        source_repository_root=Path(snapshot.repository_root),
        snapshot_commit=snapshot.start_commit_sha,
        max_iterations=4,
        max_tokens=8000,
        llm_timeout_s=30,
    )


def _store(tmp_path: Path) -> tuple[SkillImprovementStore, Path]:
    workspace = tmp_path / "workspace"
    for skill_id in SKILL_IMPROVEMENT_TARGETS:
        skill_path = workspace / "skills" / skill_id / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(
            f"---\nname: {skill_id}\n---\n\n# {skill_id}\n",
            encoding="utf-8",
        )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "devpilot.db") as connection:
        connection.execute("create table alembic_version (version_num varchar(32) not null)")
        connection.execute(
            "insert into alembic_version (version_num) values (?)",
            ("0011_p8_skill_improvement_records",),
        )
        connection.execute(
            """
            create table skill_improvement_records (
                id integer primary key autoincrement,
                record_key text not null unique,
                record_type text not null,
                record_id text not null,
                payload_json text not null,
                record_mac text not null,
                workspace text,
                project_id text,
                created_at text not null
            )
            """
        )
    return SkillImprovementStore(data_dir, workspace=workspace), workspace


def test_three_profile_target_registry_is_server_owned() -> None:
    assert set(SKILL_IMPROVEMENT_TARGETS) == {
        "repo-analysis",
        "feature-delivery",
        "bug-fix",
    }
    assert SKILL_IMPROVEMENT_TARGETS["repo-analysis"].profile_binding == "repo-analysis@1"
    assert SKILL_IMPROVEMENT_TARGETS["feature-delivery"].profile_binding == "feature-delivery@2"
    assert SKILL_IMPROVEMENT_TARGETS["bug-fix"].profile_binding == "bug-fix@1"
    assert SKILL_IMPROVEMENT_TARGETS["repo-analysis"].allowed_trigger_types == frozenset(
        {"user_confirmed_missing_fact"}
    )


def test_effective_snapshot_resolves_each_registered_skill(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)

    snapshot = store.read_effective_snapshot(workspace, skill_id="repo-analysis")

    assert snapshot.skill_id == "repo-analysis"
    assert snapshot.skill_path == "skills/repo-analysis/SKILL.md"
    assert snapshot.content_hash


def test_improvement_case_and_replay_trial_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace = _store(tmp_path)
    monkeypatch.setattr(store, "_project_is_trusted", lambda project_id, root: True)
    monkeypatch.setattr(
        "devpilot.skill_improvement.capture_git_snapshot",
        lambda project, *, allowed_file_scope, verification_profile: GitReplaySnapshot(
            repository_root=str(Path(project).resolve()),
            repository_identity="test-repository",
            start_commit_sha="a" * 40,
            working_tree_clean=True,
            allowed_file_scope=tuple(allowed_file_scope),
            verification_profile=verification_profile,
            replayable=True,
        ),
    )
    skill_hash = store.read_effective_snapshot(workspace, skill_id="repo-analysis").content_hash
    evidence = store.record_feedback(
        task_id="task-analysis-1",
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
        source_type="user_feedback",
        skill_id="repo-analysis",
        skill_hash=skill_hash,
        correction="补上测试入口。",
        actual_result="分析结果漏掉了测试入口。",
        evidence_refs=("run-analysis-1",),
    )
    snapshot = store.capture_task_snapshot(
        task_id="task-analysis-1",
        workspace=workspace,
        session_id="session-1",
        project_id="project-1",
        skill_id="repo-analysis",
        allowed_file_scope=["src/main.py"],
        verification_profile="python-syntax",
    )
    snapshot_ref = task_snapshot_ref("task-analysis-1")
    assert snapshot.task_id == "task-analysis-1"

    case = store.create_improvement_case(
        skill_id="repo-analysis",
        profile_binding="repo-analysis@1",
        origin_run_id="run-analysis-1",
        task_id="task-analysis-1",
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
        base_skill_hash=skill_hash,
        trigger_type="user_confirmed_missing_fact",
        trigger_summary="分析结果漏掉了测试入口。",
        expected_behavior="输出 tests/test_api.py。",
        evidence_refs=(evidence.evidence_id, "run-analysis-1"),
        replay_snapshot_ref=snapshot_ref,
        assertion_spec={"kind": "analysis_fact", "expected": "tests/test_api.py"},
    )

    assert case.status == "eligible"
    assert case.case_id.startswith("case-")
    assert store.get_improvement_case(case.case_id, workspace=workspace) == case
    assert store.list_improvement_cases(workspace=workspace) == [case]

    trial = store.record_replay_trial(
        case_id=case.case_id,
        skill_id="repo-analysis",
        skill_hash=skill_hash,
        version="baseline",
        model_id="deterministic-test",
        parameters={"temperature": 0},
        snapshot_ref=snapshot_ref,
        started_at="2026-09-21T00:00:00+00:00",
        finished_at="2026-09-21T00:00:01+00:00",
        token_count=42,
        duration_seconds=1.0,
        tool_trace_ref="trace-analysis-1",
        final_output_ref="output-analysis-1",
        assertion_result={"passed": True, "grader_kind": "analysis_fact"},
        status="passed",
    )

    assert trial.trial_id.startswith("trial-")
    assert trial.case_id == case.case_id
    assert trial.status in REPLAY_TRIAL_STATUSES
    assert (
        store.get_replay_trial(
            trial.trial_id,
            workspace=workspace,
            project_id="project-1",
            user_id="user-1",
            session_id="session-1",
        )
        == trial
    )
    assert store.list_replay_trials(
        case_id=case.case_id,
        workspace=workspace,
        project_id="project-1",
        user_id="user-1",
        session_id="session-1",
    ) == [trial]
    assert case.status in IMPROVEMENT_CASE_STATES

    with pytest.raises(SkillImprovementError, match="hard budget"):
        store.record_replay_trial(
            case_id=case.case_id,
            skill_id="repo-analysis",
            skill_hash=skill_hash,
            version="baseline",
            model_id="deterministic-test",
            parameters={"temperature": 0},
            snapshot_ref=snapshot_ref,
            started_at="2026-09-21T00:00:00+00:00",
            finished_at="2026-09-21T00:00:01+00:00",
            token_count=8_001,
            duration_seconds=1.0,
            tool_trace_ref="trace-over-budget",
            final_output_ref="output-over-budget",
            assertion_result={"passed": True, "grader_kind": "analysis_fact"},
            status="passed",
        )


@pytest.mark.asyncio
async def test_si08_real_agent_runner_ab_evidence_and_confirmation_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace = _store(tmp_path)
    monkeypatch.setattr(store, "_project_is_trusted", lambda project_id, root: True)
    _git_fixture(workspace)
    baseline = store.read_effective_snapshot(workspace, skill_id="repo-analysis")
    evidence = store.record_feedback(
        task_id="task-si08-real-runner",
        project_id="project-si08",
        workspace=workspace,
        user_id="user-1",
        session_id="session-si08",
        source_type="user_feedback",
        skill_id="repo-analysis",
        skill_hash=baseline.content_hash,
        correction="补上测试入口。",
        actual_result="分析结果漏掉了测试入口。",
        evidence_refs=("run-si08-real",),
    )
    snapshot = store.capture_task_snapshot(
        task_id="task-si08-real-runner",
        workspace=workspace,
        session_id="session-si08",
        project_id="project-si08",
        skill_id="repo-analysis",
        allowed_file_scope=["tests/test_api.py"],
        verification_profile="python-syntax",
        source_evidence_refs=(evidence.evidence_id,),
    )
    snapshot_ref = task_snapshot_ref("task-si08-real-runner")
    case = store.create_improvement_case(
        skill_id="repo-analysis",
        profile_binding="repo-analysis@1",
        origin_run_id="run-si08-real",
        task_id="task-si08-real-runner",
        project_id="project-si08",
        workspace=workspace,
        user_id="user-1",
        session_id="session-si08",
        base_skill_hash=baseline.content_hash,
        trigger_type="user_confirmed_missing_fact",
        trigger_summary="分析结果漏掉了测试入口。",
        expected_behavior="输出 tests/test_api.py。",
        evidence_refs=(evidence.evidence_id, "run-si08-real"),
        replay_snapshot_ref=snapshot_ref,
        assertion_spec={
            "kind": "analysis_fact",
            "expected": "tests/test_api.py",
            "expected_paths": ("tests/test_api.py",),
            "expected_evidence_refs": (evidence.evidence_id,),
        },
    )
    candidate = store.create_candidate(
        user_id="user-1",
        project_id="project-si08",
        skill_path="skills/repo-analysis/SKILL.md",
        baseline_content=baseline.content,
        candidate_content=baseline.content + "\n- 输出测试入口。\n",
        evidence_ids=(evidence.evidence_id,),
        case_ids=(case.case_id,),
        reason="补充测试入口识别。",
        expected_behavior="输出 tests/test_api.py。",
        test_set_version="si-07-real-replay-v1",
        workspace=workspace,
    )
    snapshot_for_replay = GitReplaySnapshot(
        repository_root=str(snapshot.repository_root),
        repository_identity=str(snapshot.repository_identity),
        start_commit_sha=snapshot.start_commit_sha,
        working_tree_clean=True,
        allowed_file_scope=snapshot.allowed_file_scope,
        verification_profile=str(snapshot.verification_profile),
        replayable=True,
    )
    private_root = tmp_path / "private-replays"
    baseline_request = _si08_request(
        snapshot=snapshot_for_replay,
        snapshot_ref=snapshot_ref,
        private_root=private_root,
        provider=_SI08Provider(candidate=False),
        version="baseline",
        evidence_ref=evidence.evidence_id,
    )
    candidate_request = _si08_request(
        snapshot=snapshot_for_replay,
        snapshot_ref=snapshot_ref,
        private_root=private_root,
        provider=_SI08Provider(candidate=True),
        version="candidate",
        evidence_ref=evidence.evidence_id,
    )

    report = await store.evaluate_candidate_replay(
        candidate_id=candidate.candidate_id,
        case_id=case.case_id,
        baseline_request=baseline_request,
        candidate_request=candidate_request,
        baseline_skill=baseline.content,
        candidate_skill=candidate.candidate_content,
        workspace=str(workspace),
        user_id="user-1",
        project_id="project-si08",
        expected_paths=("tests/test_api.py",),
        expected_evidence_refs=(evidence.evidence_id,),
        fixture_kind="synthetic_git_fixture",
    )

    assert report.eligible is True
    assert report.baseline_successes == 0
    assert report.candidate_successes == 1
    assert report.parameters["fixture_kind"] == "synthetic_git_fixture"
    assert store.get_candidate(candidate.candidate_id).status == "pending_confirmation"
    assert not list(private_root.glob("replay-*"))

    old_task = store.capture_task_snapshot(
        task_id="task-si08-old-after-replay",
        workspace=workspace,
        session_id="session-old",
        project_id="project-si08",
        skill_id="repo-analysis",
    )
    confirmation = store.issue_confirmation(
        candidate.candidate_id,
        user_id="user-1",
        session_id="session-confirm",
        workspace=workspace,
        project_id="project-si08",
    )
    active = store.activate_candidate(
        confirmation.confirmation_id,
        workspace=workspace,
        user_id="user-1",
        session_id="session-confirm",
        project_id="project-si08",
    )
    assert active.content_hash == candidate.candidate_hash
    assert (
        store.capture_task_snapshot(
            task_id="task-si08-old-after-replay",
            workspace=workspace,
            session_id="session-old",
            project_id="project-si08",
            skill_id="repo-analysis",
        ).content_hash
        == old_task.content_hash
    )
    assert (
        store.capture_task_snapshot(
            task_id="task-si08-new-after-replay",
            workspace=workspace,
            session_id="session-new",
            project_id="project-si08",
            skill_id="repo-analysis",
        ).content_hash
        == candidate.candidate_hash
    )
    with pytest.raises(SkillImprovementError, match="already consumed"):
        store.activate_candidate(
            confirmation.confirmation_id,
            workspace=workspace,
            user_id="user-1",
            session_id="session-confirm",
            project_id="project-si08",
        )
    restored = store.restore_candidate(
        candidate.candidate_id,
        workspace=workspace,
        user_id="user-1",
        project_id="project-si08",
    )
    assert restored.content_hash == old_task.content_hash


def test_real_replay_evaluation_binds_baseline_candidate_and_stays_inactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace = _store(tmp_path)
    monkeypatch.setattr(store, "_project_is_trusted", lambda project_id, root: True)
    monkeypatch.setattr(
        "devpilot.skill_improvement.capture_git_snapshot",
        lambda project, *, allowed_file_scope, verification_profile: GitReplaySnapshot(
            repository_root=str(Path(project).resolve()),
            repository_identity="test-repository",
            start_commit_sha="b" * 40,
            working_tree_clean=True,
            allowed_file_scope=tuple(allowed_file_scope),
            verification_profile=verification_profile,
            replayable=True,
        ),
    )
    baseline = store.read_effective_snapshot(workspace, skill_id="repo-analysis")
    evidence = store.record_feedback(
        task_id="task-replay-evaluation",
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-replay-evaluation",
        source_type="user_feedback",
        skill_id="repo-analysis",
        skill_hash=baseline.content_hash,
        correction="补上测试入口。",
        actual_result="分析结果漏掉了测试入口。",
        evidence_refs=("run-replay-evaluation",),
    )
    store.capture_task_snapshot(
        task_id="task-replay-evaluation",
        workspace=workspace,
        session_id="session-replay-evaluation",
        project_id="project-1",
        skill_id="repo-analysis",
        allowed_file_scope=["src/main.py"],
        verification_profile="python-syntax",
    )
    snapshot_ref = task_snapshot_ref("task-replay-evaluation")
    case = store.create_improvement_case(
        skill_id="repo-analysis",
        profile_binding="repo-analysis@1",
        origin_run_id="run-replay-evaluation",
        task_id="task-replay-evaluation",
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-replay-evaluation",
        base_skill_hash=baseline.content_hash,
        trigger_type="user_confirmed_missing_fact",
        trigger_summary="分析结果漏掉了测试入口。",
        expected_behavior="输出 tests/test_api.py。",
        evidence_refs=(evidence.evidence_id, "run-replay-evaluation"),
        replay_snapshot_ref=snapshot_ref,
        assertion_spec={"kind": "analysis_fact", "expected": "tests/test_api.py"},
    )
    candidate = store.create_candidate(
        user_id="user-1",
        project_id="project-1",
        skill_path="skills/repo-analysis/SKILL.md",
        baseline_content=baseline.content,
        candidate_content=baseline.content + "\n- 输出测试入口。\n",
        evidence_ids=(evidence.evidence_id,),
        case_ids=(case.case_id,),
        reason="补充测试入口识别。",
        expected_behavior="输出 tests/test_api.py。",
        test_set_version="si-07-real-replay-v1",
        workspace=workspace,
    )
    baseline_trial = store.record_replay_trial(
        case_id=case.case_id,
        skill_id="repo-analysis",
        skill_hash=baseline.content_hash,
        version="baseline",
        model_id="deterministic-test",
        parameters={"temperature": 0},
        snapshot_ref=snapshot_ref,
        started_at="2026-09-22T00:00:00+00:00",
        finished_at="2026-09-22T00:00:01+00:00",
        token_count=3,
        duration_seconds=1.0,
        tool_trace_ref="trace-baseline",
        final_output_ref="output-baseline",
        assertion_result={
            "passed": False,
            "grader_kind": "analysis_fact",
            "reason": "missing test entry",
        },
        status="failed",
    )
    candidate_trial = store.record_replay_trial(
        case_id=case.case_id,
        skill_id="repo-analysis",
        skill_hash=candidate.candidate_hash,
        version="candidate",
        model_id="deterministic-test",
        parameters={"temperature": 0},
        snapshot_ref=snapshot_ref,
        started_at="2026-09-22T00:00:02+00:00",
        finished_at="2026-09-22T00:00:03+00:00",
        token_count=3,
        duration_seconds=1.0,
        tool_trace_ref="trace-candidate",
        final_output_ref="output-candidate",
        assertion_result={
            "passed": True,
            "grader_kind": "analysis_fact",
            "reason": "test entry present",
        },
        status="passed",
        candidate_id=candidate.candidate_id,
    )

    report = store.record_replay_evaluation(
        candidate.candidate_id,
        case_id=case.case_id,
        baseline_trial_id=baseline_trial.trial_id,
        candidate_trial_id=candidate_trial.trial_id,
        workspace=workspace,
        user_id="user-1",
        project_id="project-1",
    )

    assert report.eligible is True
    assert report.test_set_version == "si-07-real-replay-v1"
    assert report.template == "agent-runner-replay-v1"
    assert store.get_evaluation(report.evaluation_id) == report
    saved_candidate = store.get_candidate(
        candidate.candidate_id,
        workspace=workspace,
        user_id="user-1",
        project_id="project-1",
    )
    assert saved_candidate is not None
    assert saved_candidate.status == "pending_confirmation"

    # SI-08 closes the real-Replay path through the existing confirmation and
    # snapshot boundaries: evaluation alone never activates, activation is
    # one-time and only new tasks observe the candidate, then restore returns
    # the stable baseline.
    old_task = store.capture_task_snapshot(
        task_id="task-before-si08-confirmation",
        workspace=workspace,
        session_id="session-1",
        project_id="project-1",
        skill_id="repo-analysis",
    )
    confirmation = store.issue_confirmation(
        candidate.candidate_id,
        user_id="user-1",
        session_id="session-1",
        workspace=workspace,
        project_id="project-1",
    )
    active = store.activate_candidate(
        confirmation.confirmation_id,
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
        project_id="project-1",
    )
    assert active.content_hash == candidate.candidate_hash
    with pytest.raises(SkillImprovementError, match="already consumed"):
        store.activate_candidate(
            confirmation.confirmation_id,
            workspace=workspace,
            user_id="user-1",
            session_id="session-1",
            project_id="project-1",
        )
    assert (
        store.capture_task_snapshot(
            task_id="task-before-si08-confirmation",
            workspace=workspace,
            session_id="session-1",
            project_id="project-1",
            skill_id="repo-analysis",
        ).content_hash
        == old_task.content_hash
    )
    new_task = store.capture_task_snapshot(
        task_id="task-after-si08-confirmation",
        workspace=workspace,
        session_id="session-2",
        project_id="project-1",
        skill_id="repo-analysis",
    )
    assert new_task.content_hash == candidate.candidate_hash
    restored = store.restore_candidate(
        candidate.candidate_id,
        workspace=workspace,
        user_id="user-1",
        project_id="project-1",
    )
    assert restored.content_hash == old_task.content_hash
    assert store.get_candidate(candidate.candidate_id).status == "stale"

    over_budget_candidate = store.create_candidate(
        user_id="user-1",
        project_id="project-1",
        skill_path="skills/repo-analysis/SKILL.md",
        baseline_content=baseline.content,
        candidate_content=baseline.content + "\n- 输出完整测试入口。\n",
        evidence_ids=(evidence.evidence_id,),
        case_ids=(case.case_id,),
        reason="验证聚合预算门禁。",
        expected_behavior="输出 tests/test_api.py。",
        test_set_version="si-07-real-replay-v1",
        workspace=workspace,
    )
    over_budget_trial = store.record_replay_trial(
        case_id=case.case_id,
        skill_id="repo-analysis",
        skill_hash=over_budget_candidate.candidate_hash,
        version="candidate",
        model_id="deterministic-test",
        parameters={"temperature": 0},
        snapshot_ref=snapshot_ref,
        started_at="2026-09-22T00:00:04+00:00",
        finished_at="2026-09-22T00:00:05+00:00",
        token_count=3,
        duration_seconds=1.0,
        tool_trace_ref="trace-over-budget-aggregate",
        final_output_ref="output-over-budget-aggregate",
        assertion_result={"passed": True, "grader_kind": "analysis_fact"},
        status="passed",
        candidate_id=over_budget_candidate.candidate_id,
    )
    original_get_trial = store.get_replay_trial

    def get_legacy_over_budget_trial(trial_id: str, **kwargs: object):
        trial = original_get_trial(trial_id, **kwargs)
        if trial is not None and trial_id == over_budget_trial.trial_id:
            return replace(trial, token_count=13_000)
        return trial

    monkeypatch.setattr(store, "get_replay_trial", get_legacy_over_budget_trial)
    over_budget_report = store.record_replay_evaluation(
        over_budget_candidate.candidate_id,
        case_id=case.case_id,
        baseline_trial_id=baseline_trial.trial_id,
        candidate_trial_id=over_budget_trial.trial_id,
        workspace=workspace,
        user_id="user-1",
        project_id="project-1",
    )
    assert over_budget_report.eligible is False


def test_non_replayable_snapshot_is_recorded_without_model_eligibility(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    skill_hash = store.read_effective_snapshot(workspace, skill_id="repo-analysis").content_hash
    snapshot = store.capture_task_snapshot(
        task_id="task-non-replayable",
        workspace=workspace,
        session_id="session-1",
        project_id="project-1",
        skill_id="repo-analysis",
        allowed_file_scope=["src/main.py"],
        verification_profile="python-syntax",
    )
    assert snapshot.replayable is False
    assert snapshot.working_tree_clean is False
    assert snapshot.replayability_reason in {
        "working tree is not clean",
        "git repository metadata is unavailable",
        "project is not registered and trusted",
    }
    assert snapshot.allowed_file_scope == ("src/main.py",)

    evidence = store.record_feedback(
        task_id="task-non-replayable",
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
        source_type="user_feedback",
        skill_id="repo-analysis",
        skill_hash=skill_hash,
        correction="补上测试入口。",
        actual_result="分析结果漏掉了测试入口。",
        evidence_refs=("run-non-replayable",),
    )
    case = store.create_improvement_case(
        skill_id="repo-analysis",
        profile_binding="repo-analysis@1",
        origin_run_id="run-non-replayable",
        task_id="task-non-replayable",
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
        base_skill_hash=skill_hash,
        trigger_type="user_confirmed_missing_fact",
        trigger_summary="分析结果漏掉了测试入口。",
        expected_behavior="输出测试入口。",
        evidence_refs=(evidence.evidence_id, "run-non-replayable"),
        replay_snapshot_ref=task_snapshot_ref("task-non-replayable"),
        assertion_spec={"kind": "analysis_fact"},
    )

    assert case.status == "not_replayable"
    assert case.replay_snapshot_ref == task_snapshot_ref("task-non-replayable")


def test_placeholder_migration_cannot_upgrade_start_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace = _store(tmp_path)
    monkeypatch.setattr(store, "_project_is_trusted", lambda project_id, root: True)
    snapshots = iter(
        [
            GitReplaySnapshot(
                repository_root=str(workspace),
                repository_identity="repo",
                start_commit_sha="a" * 40,
                working_tree_clean=False,
                allowed_file_scope=(),
                verification_profile="pending",
                replayable=False,
                reason="working tree is not clean",
            ),
            GitReplaySnapshot(
                repository_root=str(workspace),
                repository_identity="repo",
                start_commit_sha="b" * 40,
                working_tree_clean=True,
                allowed_file_scope=("src/main.py",),
                verification_profile="python-syntax",
                replayable=True,
            ),
        ]
    )
    monkeypatch.setattr(
        "devpilot.skill_improvement.capture_git_snapshot",
        lambda *args, **kwargs: next(snapshots),
    )

    pending = store.capture_task_snapshot(
        task_id="task-placeholder",
        workspace=workspace,
        session_id="session-1",
        project_id="project-1",
        skill_id="repo-analysis",
        verification_profile="pending",
        source_evidence_refs=["run-start"],
    )
    migrated = store.capture_task_snapshot(
        task_id="task-placeholder",
        workspace=workspace,
        session_id="session-1",
        project_id="project-1",
        skill_id="repo-analysis",
        allowed_file_scope=["src/main.py"],
        verification_profile="python-syntax",
        source_evidence_refs=["run-verification"],
    )

    assert pending.start_commit_sha == "a" * 40
    assert migrated.start_commit_sha == "a" * 40
    assert migrated.replayable is False
    assert migrated.replayability_reason == "working tree is not clean"
    assert migrated.source_evidence_refs == ("run-start", "run-verification")


def test_project_trust_checks_real_registration_and_allowed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace = _store(tmp_path)
    monkeypatch.delenv("DEVPILOT_ALLOWED_ROOTS", raising=False)
    fingerprint = repository_fingerprint(workspace)
    root_id = allowed_root_id(workspace)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "create table projects (id text primary key, real_path text, trust_state text, "
            "repo_fingerprint text, allowed_root_id text)"
        )
        connection.execute(
            "insert into projects values (?, ?, ?, ?, ?)",
            ("project-1", str(workspace), "trusted", fingerprint, root_id),
        )

    assert store._project_is_trusted("project-1", workspace) is True
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "update projects set allowed_root_id = ? where id = ?",
            ("wrong-root", "project-1"),
        )
    assert store._project_is_trusted("project-1", workspace) is False


@pytest.mark.parametrize(
    ("label", "start_commit_sha", "working_tree_clean", "reason"),
    [
        ("dirty", "a" * 40, False, "working tree is not clean"),
        ("non-git", None, False, "git repository metadata is unavailable"),
        ("missing-commit", None, True, "repository has no resolvable start commit"),
    ],
)
def test_each_git_failure_kind_becomes_a_non_replayable_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    start_commit_sha: str | None,
    working_tree_clean: bool,
    reason: str,
) -> None:
    store, workspace = _store(tmp_path)
    monkeypatch.setattr(store, "_project_is_trusted", lambda project_id, root: True)
    monkeypatch.setattr(
        "devpilot.skill_improvement.capture_git_snapshot",
        lambda *args, **kwargs: GitReplaySnapshot(
            repository_root=str(workspace),
            repository_identity="repo",
            start_commit_sha=start_commit_sha,
            working_tree_clean=working_tree_clean,
            allowed_file_scope=("src/main.py",),
            verification_profile="python-syntax",
            replayable=False,
            reason=reason,
        ),
    )
    task_id = f"task-{label}"
    origin = f"run-{label}"
    skill_hash = store.read_effective_snapshot(workspace, skill_id="repo-analysis").content_hash
    evidence = store.record_feedback(
        task_id=task_id,
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id=f"session-{label}",
        source_type="user_feedback",
        skill_id="repo-analysis",
        skill_hash=skill_hash,
        correction="补上测试入口。",
        actual_result="分析结果漏掉了测试入口。",
        evidence_refs=(origin,),
    )
    snapshot = store.capture_task_snapshot(
        task_id=task_id,
        workspace=workspace,
        session_id=f"session-{label}",
        project_id="project-1",
        skill_id="repo-analysis",
        allowed_file_scope=["src/main.py"],
        verification_profile="python-syntax",
    )
    case = store.create_improvement_case(
        skill_id="repo-analysis",
        profile_binding="repo-analysis@1",
        origin_run_id=origin,
        task_id=task_id,
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id=f"session-{label}",
        base_skill_hash=skill_hash,
        trigger_type="user_confirmed_missing_fact",
        trigger_summary="分析结果漏掉了测试入口。",
        expected_behavior="输出测试入口。",
        evidence_refs=(evidence.evidence_id, origin),
        replay_snapshot_ref=task_snapshot_ref(task_id),
        assertion_spec={"kind": "analysis_fact"},
    )

    assert snapshot.replayability_reason == reason
    assert case.status == "not_replayable"
    assert store.list_improvement_cases(status="eligible") == []


def test_improvement_case_rejects_unverifiable_refs_and_is_idempotent(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    skill_hash = store.read_effective_snapshot(workspace, skill_id="repo-analysis").content_hash
    snapshot = store.capture_task_snapshot(
        task_id="task-analysis-1",
        workspace=workspace,
        session_id="session-1",
        project_id="project-1",
        skill_id="repo-analysis",
    )
    kwargs = {
        "skill_id": "repo-analysis",
        "profile_binding": "repo-analysis@1",
        "origin_run_id": "run-analysis-1",
        "task_id": "task-analysis-1",
        "project_id": "project-1",
        "workspace": workspace,
        "user_id": "user-1",
        "session_id": "session-1",
        "base_skill_hash": skill_hash,
        "trigger_type": "user_confirmed_missing_fact",
        "trigger_summary": "缺了测试入口。",
        "expected_behavior": "输出测试入口。",
        "evidence_refs": ("run-analysis-1",),
        "replay_snapshot_ref": task_snapshot_ref("task-analysis-1"),
        "assertion_spec": {"kind": "analysis_fact"},
    }
    with pytest.raises(SkillImprovementError, match="unavailable"):
        store.create_improvement_case(**kwargs)

    evidence = store.record_feedback(
        task_id="task-analysis-1",
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
        source_type="user_feedback",
        skill_id="repo-analysis",
        skill_hash=skill_hash,
        correction="补上测试入口。",
        actual_result="分析结果漏掉了测试入口。",
        evidence_refs=("run-analysis-1",),
    )
    tool_evidence = store.record_feedback(
        task_id="task-analysis-1",
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
        source_type="tool_result",
        skill_id="repo-analysis",
        skill_hash=skill_hash,
        correction="工具输出声称分析完成。",
        actual_result="没有受信用户确认。",
        evidence_refs=("run-analysis-1",),
    )
    with pytest.raises(SkillImprovementError, match="source is not trusted"):
        store.create_improvement_case(
            **{
                **kwargs,
                "evidence_refs": (tool_evidence.evidence_id,),
            }
        )
    kwargs["evidence_refs"] = (evidence.evidence_id,)
    case = store.create_improvement_case(**kwargs)
    assert case == store.create_improvement_case(**kwargs)
    assert snapshot.content_hash == case.base_skill_hash


def test_fake_trusted_verification_and_unscoped_trial_reads_are_rejected(
    tmp_path: Path,
) -> None:
    store, workspace = _store(tmp_path)
    skill_hash = store.read_effective_snapshot(workspace, skill_id="feature-delivery").content_hash
    with pytest.raises(SkillImprovementError, match="server-owned fact"):
        store.record_feedback(
            task_id="task-delivery-1",
            project_id="project-1",
            workspace=workspace,
            user_id="user-1",
            session_id="session-1",
            source_type="trusted_verification",
            skill_id="feature-delivery",
            skill_hash=skill_hash,
            correction="验证失败。",
            actual_result="伪造的服务端验证引用。",
            evidence_refs=(
                "run-delivery-1",
                "verification-run-delivery-1:change-1",
            ),
        )
    with pytest.raises(SkillImprovementError, match="scope is required"):
        store.list_replay_trials()


def test_failure_evidence_is_bound_to_the_origin_task_and_workspace(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    database = store.database_path
    workspace_value = str(workspace.resolve())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "create table projects (id text primary key, real_path text not null)"
        )
        connection.execute(
            "create table runs (id text primary key, project_id text not null, "
            "kind text not null, status text not null)"
        )
        connection.execute(
            "create table run_events (id integer primary key autoincrement, run_id text not null, "
            "event_type text not null, payload_json text not null)"
        )
        connection.execute("insert into projects values (?, ?)", ("project-1", workspace_value))
        connection.execute(
            "insert into runs values (?, ?, ?, ?)",
            ("failure-run-1", "project-1", "bug-check", "failed"),
        )
        connection.execute(
            "insert into run_events (run_id, event_type, payload_json) values (?, ?, ?)",
            (
                "failure-run-1",
                "bug_check.failed",
                '{"expected_behavior":"price returns a positive value"}',
            ),
        )
        connection.execute(
            "insert into run_events (run_id, event_type, payload_json) values (?, ?, ?)",
            (
                "failure-run-1",
                "run.context.bound",
                '{"task_id":"task-1","session_id":"session-1",'
                '"user_id":"user-1","workspace":"'
                + workspace_value.replace("\\", "\\\\")
                + '"}',
            ),
        )
        connection.execute(
            "insert into runs values (?, ?, ?, ?)",
            ("failure-run-2", "project-1", "bug-check", "failed"),
        )
        connection.execute(
            "insert into run_events (run_id, event_type, payload_json) values (?, ?, ?)",
            (
                "failure-run-2",
                "bug_check.failed",
                '{"expected_behavior":"price returns a positive value"}',
            ),
        )
        connection.execute(
            "insert into run_events (run_id, event_type, payload_json) values (?, ?, ?)",
            (
                "failure-run-2",
                "run.context.bound",
                '{"task_id":"task-other","session_id":"session-other",'
                '"user_id":"user-other","workspace":"'
                + workspace_value.replace("\\", "\\\\")
                + '"}',
            ),
        )

    failure_ref = "failure-failure-run-1"
    assert store._has_trusted_failure_fact(  # pyright: ignore[reportPrivateUsage]
        failure_ref=failure_ref,
        project_id="project-1",
        expected_behavior="price returns a positive value",
        task_id="task-1",
        session_id="session-1",
        user_id="user-1",
        workspace=workspace_value,
    )
    assert not store._has_trusted_failure_fact(  # pyright: ignore[reportPrivateUsage]
        failure_ref=failure_ref,
        project_id="project-1",
        expected_behavior="price returns a positive value",
        task_id="task-2",
        session_id="session-1",
        user_id="user-1",
        workspace=workspace_value,
    )
    assert not store._has_trusted_failure_fact(  # pyright: ignore[reportPrivateUsage]
        failure_ref=failure_ref,
        project_id="project-1",
        expected_behavior="price returns a positive value",
        task_id="task-1",
        session_id="session-other",
        user_id="user-1",
        workspace=workspace_value,
    )
    assert not store._has_trusted_failure_fact(  # pyright: ignore[reportPrivateUsage]
        failure_ref=failure_ref,
        project_id="project-1",
        expected_behavior="price returns a positive value",
        task_id="task-1",
        session_id="session-1",
        user_id="user-other",
        workspace=workspace_value,
    )
    assert not store._has_trusted_failure_fact(  # pyright: ignore[reportPrivateUsage]
        failure_ref=failure_ref,
        project_id="project-1",
        expected_behavior="price returns a positive value",
        task_id="task-1",
        session_id="session-1",
        user_id="user-1",
        workspace=str((tmp_path / "other-workspace").resolve()),
    )
    assert not store._has_trusted_failure_fact(  # pyright: ignore[reportPrivateUsage]
        failure_ref="failure-failure-run-2",
        project_id="project-1",
        expected_behavior="price returns a positive value",
        task_id="task-1",
        session_id="session-1",
        user_id="user-1",
        workspace=workspace_value,
    )
    assert not store._has_trusted_failure_fact(  # pyright: ignore[reportPrivateUsage]
        failure_ref="failure-failure-run-2",
        project_id="project-1",
        expected_behavior="price returns a positive value",
        task_id="task-1",
        session_id="session-1",
        user_id="user-1",
        workspace=workspace_value,
    )


def test_legacy_snapshot_id_is_readable_and_snapshot_invalidation_propagates(
    tmp_path: Path,
) -> None:
    store, workspace = _store(tmp_path)
    skill = store.read_effective_snapshot(workspace, skill_id="repo-analysis")
    task_id = "task-analysis-legacy"
    legacy_ref = content_sha256(task_id)
    store._atomic_write(
        store.snapshots_dir / f"{legacy_ref}.json",
        {
            "task_id": task_id,
            "session_id": "session-1",
            "project_id": "project-1",
            "workspace": str(workspace.resolve()),
            **store._snapshot_to_dict(skill),
        },
    )
    assert store.get_task_snapshot(
        legacy_ref,
        workspace=workspace,
        project_id="project-1",
        session_id="session-1",
        skill_id="repo-analysis",
    ) is not None
    evidence = store.record_feedback(
        task_id=task_id,
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
        source_type="user_feedback",
        skill_id="repo-analysis",
        skill_hash=skill.content_hash,
        correction="补上测试入口。",
        actual_result="分析结果漏掉了测试入口。",
        evidence_refs=("run-analysis-legacy",),
    )
    case = store.create_improvement_case(
        skill_id="repo-analysis",
        profile_binding="repo-analysis@1",
        origin_run_id="run-analysis-legacy",
        task_id=task_id,
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
        base_skill_hash=skill.content_hash,
        trigger_type="user_confirmed_missing_fact",
        trigger_summary="缺了测试入口。",
        expected_behavior="输出测试入口。",
        evidence_refs=(evidence.evidence_id,),
        replay_snapshot_ref=legacy_ref,
        assertion_spec={"kind": "analysis_fact"},
    )
    store.invalidate_task_snapshot(legacy_ref)
    invalidated = store.get_improvement_case(case.case_id)
    assert invalidated is not None
    assert invalidated.status == "not_replayable"


def test_delete_feedback_blocks_linked_case_and_replay(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    skill_hash = store.read_effective_snapshot(workspace, skill_id="repo-analysis").content_hash
    evidence = store.record_feedback(
        task_id="task-analysis-1",
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
        source_type="user_feedback",
        skill_id="repo-analysis",
        skill_hash=skill_hash,
        correction="补上测试入口。",
        actual_result="分析结果漏掉了测试入口。",
        evidence_refs=("run-analysis-1",),
    )
    store.capture_task_snapshot(
        task_id="task-analysis-1",
        workspace=workspace,
        session_id="session-1",
        project_id="project-1",
        skill_id="repo-analysis",
    )
    case = store.create_improvement_case(
        skill_id="repo-analysis",
        profile_binding="repo-analysis@1",
        origin_run_id="run-analysis-1",
        task_id="task-analysis-1",
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
        base_skill_hash=skill_hash,
        trigger_type="user_confirmed_missing_fact",
        trigger_summary="缺了测试入口。",
        expected_behavior="输出测试入口。",
        evidence_refs=(evidence.evidence_id,),
        replay_snapshot_ref=task_snapshot_ref("task-analysis-1"),
        assertion_spec={"kind": "analysis_fact"},
    )
    store.delete_feedback(
        evidence.evidence_id,
        workspace=workspace,
        user_id="user-1",
        project_id="project-1",
    )
    invalidated = store.get_improvement_case(case.case_id)
    assert invalidated is not None
    assert invalidated.status == "not_replayable"
    with pytest.raises(SkillImprovementError, match="no longer replayable"):
        store.record_replay_trial(
            case_id=case.case_id,
            skill_id="repo-analysis",
            skill_hash=skill_hash,
            version="baseline",
            model_id="deterministic-test",
            parameters={},
            snapshot_ref=task_snapshot_ref("task-analysis-1"),
            started_at="2026-09-21T00:00:00+00:00",
            finished_at="2026-09-21T00:00:01+00:00",
            token_count=1,
            duration_seconds=1.0,
            tool_trace_ref="trace-analysis-1",
            final_output_ref="output-analysis-1",
            assertion_result={"passed": True, "grader_kind": "analysis_fact"},
            status="passed",
        )
