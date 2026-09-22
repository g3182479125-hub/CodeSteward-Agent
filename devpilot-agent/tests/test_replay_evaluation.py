from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_production_replay_orchestrator_persists_both_trials_and_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("loguru")
    from nanobot.agent.skill_replay import ReplayComparison, ReplayTrialResult

    from devpilot import replay_evaluation
    from devpilot.replay_evaluation import evaluate_candidate_replay
    from devpilot.skill_improvement import content_sha256

    candidate = SimpleNamespace(
        candidate_id="candidate-1",
        skill_id="repo-analysis",
        candidate_hash=content_sha256("candidate"),
        case_ids=("case-1",),
    )
    case = SimpleNamespace(
        case_id="case-1",
        skill_id="repo-analysis",
        task_id="task-1",
        base_skill_hash=content_sha256("baseline"),
        replay_snapshot_ref="snapshot-task-1",
        allowed_file_scope=("src/main.py",),
        workspace="/workspace/project",
        project_id="project-1",
        session_id="session-1",
        profile_binding="repo-analysis@1",
        expected_behavior="",
        assertion_spec={},
    )
    snapshot = SimpleNamespace(
        replayable=True,
        allowed_file_scope=("src/main.py",),
        repository_root="/workspace/project",
        start_commit_sha="a" * 40,
    )
    baseline_result = ReplayTrialResult(
        status="passed",
        final_output="old",
        tool_events=(),
        tools_used=(),
        usage={"total_tokens": 2},
        duration_seconds=0.1,
        changed_paths=(),
        worktree_path="private/baseline",
    )
    candidate_result = ReplayTrialResult(
        status="passed",
        final_output="new",
        tool_events=(),
        tools_used=(),
        usage={"total_tokens": 3},
        duration_seconds=0.2,
        changed_paths=(),
        worktree_path="private/candidate",
    )

    class FakeStore:
        def __init__(self) -> None:
            self.trials: list[dict[str, object]] = []

        def get_candidate(self, *_args, **_kwargs):
            return candidate

        def get_improvement_case(self, *_args, **_kwargs):
            return case

        def get_task_snapshot(self, *_args, **_kwargs):
            return snapshot

        def record_replay_trial(self, **kwargs):
            self.trials.append(kwargs)

        def record_replay_evaluation(self, *args, **kwargs):
            return SimpleNamespace(
                candidate_id=args[0],
                baseline_trial_id=kwargs["baseline_trial_id"],
                candidate_trial_id=kwargs["candidate_trial_id"],
            )

    async def fake_comparison(*_args, **_kwargs):
        return ReplayComparison(baseline_result, candidate_result)

    def fake_grade(request, result, **_kwargs):
        return SimpleNamespace(
            grader_kind="analysis_fact",
            passed=result is candidate_result,
            reason=(
                "candidate fact present"
                if result is candidate_result
                else "baseline missing fact"
            ),
        )

    monkeypatch.setattr(replay_evaluation, "run_replay_comparison", fake_comparison)
    monkeypatch.setattr(replay_evaluation, "grade_replay", fake_grade)
    request = SimpleNamespace(
        model_id="fake/model",
        max_iterations=8,
        task_id="task-1",
        project_id="project-1",
        profile_name="repo-analysis",
        skill_id="repo-analysis",
        snapshot_ref="snapshot-task-1",
        snapshot_commit="a" * 40,
        source_repository_root=Path("/workspace/project"),
        private_worktree_root=Path("/private-replays"),
    )
    store = FakeStore()

    report = await evaluate_candidate_replay(
        store,
        candidate_id="candidate-1",
        case_id="case-1",
        baseline_request=request,
        candidate_request=request,
        baseline_skill="baseline",
        candidate_skill="candidate",
        workspace="/workspace/project",
        user_id="user-1",
        project_id="project-1",
    )

    assert len(store.trials) == 2
    assert {item["version"] for item in store.trials} == {"baseline", "candidate"}
    assert store.trials[0]["candidate_id"] is None
    assert store.trials[1]["candidate_id"] == "candidate-1"
    assert report.candidate_id == "candidate-1"
