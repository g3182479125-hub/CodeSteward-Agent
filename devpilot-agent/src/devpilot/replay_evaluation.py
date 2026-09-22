"""Production orchestration for one SI-07 baseline/candidate comparison."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from nanobot.agent.skill_replay import (
    ReplayRequest,
    ReplayTrialResult,
    grade_replay,
    run_replay_comparison,
)

from .skill_improvement import EvaluationReport, SkillImprovementError, SkillImprovementStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _trial_status(result: ReplayTrialResult, passed: bool) -> str:
    if result.status != "passed":
        return result.status
    return "passed" if passed else "failed"


async def evaluate_candidate_replay(
    store: SkillImprovementStore,
    *,
    candidate_id: str,
    case_id: str,
    baseline_request: ReplayRequest,
    candidate_request: ReplayRequest,
    baseline_skill: str,
    candidate_skill: str,
    workspace: str,
    user_id: str,
    project_id: str,
    expected_behavior: str | None = None,
    expected_paths: tuple[str, ...] = (),
    expected_evidence_refs: tuple[str, ...] = (),
    baseline_verification_passed: bool = False,
    candidate_verification_passed: bool = False,
    baseline_failure_repaired: bool = False,
    candidate_failure_repaired: bool = False,
) -> EvaluationReport:
    """Execute, grade, and persist one real SI-07 comparison.

    The request factories and verification facts are server-owned inputs. The
    candidate Skill can change the AgentRunner transcript, but never the
    grader, Case binding, or final EvaluationReport status.
    """

    candidate = store.get_candidate(
        candidate_id,
        workspace=workspace,
        user_id=user_id,
        project_id=project_id,
    )
    case = store.get_improvement_case(case_id)
    if candidate is None or case is None:
        raise SkillImprovementError("replay candidate or case was not found")
    if case_id not in candidate.case_ids:
        raise SkillImprovementError("replay case is not bound to the candidate")
    if case.replay_snapshot_ref is None:
        raise SkillImprovementError("replay case has no snapshot")
    snapshot = store.get_task_snapshot(
        case.replay_snapshot_ref,
        workspace=case.workspace,
        project_id=case.project_id,
        session_id=case.session_id,
        skill_id=case.skill_id,
    )
    if snapshot is None or not snapshot.replayable:
        raise SkillImprovementError("replay case snapshot is not usable")

    comparison = await run_replay_comparison(
        baseline_request,
        candidate_request,
        baseline_skill=baseline_skill,
        candidate_skill=candidate_skill,
    )
    baseline_grade = grade_replay(
        baseline_request,
        comparison.baseline,
        expected_behavior=expected_behavior,
        expected_paths=expected_paths,
        expected_evidence_refs=expected_evidence_refs,
        trusted_verification_passed=baseline_verification_passed,
        original_failure_repaired=baseline_failure_repaired,
        allowed_file_scope=snapshot.allowed_file_scope,
    )
    candidate_grade = grade_replay(
        candidate_request,
        comparison.candidate,
        expected_behavior=expected_behavior,
        expected_paths=expected_paths,
        expected_evidence_refs=expected_evidence_refs,
        trusted_verification_passed=candidate_verification_passed,
        original_failure_repaired=candidate_failure_repaired,
        allowed_file_scope=snapshot.allowed_file_scope,
    )

    def record(result: ReplayTrialResult, grade_passed: bool, reason: str, version: str) -> str:
        trial_id = f"trial-{uuid.uuid4()}"
        store.record_replay_trial(
            case_id=case_id,
            skill_id=candidate.skill_id,
            skill_hash=case.base_skill_hash if version == "baseline" else candidate.candidate_hash,
            version=version,
            model_id=(
                baseline_request.model_id
                if version == "baseline"
                else candidate_request.model_id
            ),
            parameters={"max_iterations": baseline_request.max_iterations},
            snapshot_ref=case.replay_snapshot_ref,
            started_at=_now(),
            finished_at=_now(),
            token_count=result.token_count,
            duration_seconds=result.duration_seconds,
            tool_trace_ref=f"trace-{uuid.uuid4()}",
            final_output_ref=f"output-{uuid.uuid4()}",
            assertion_result={
                "passed": grade_passed,
                "grader_kind": grade_kind,
                "reason": reason,
                "changed_paths": list(result.changed_paths),
            },
            status=_trial_status(result, grade_passed),
            trial_id=trial_id,
            candidate_id=candidate_id if version == "candidate" else None,
        )
        return trial_id

    grade_kind = candidate_grade.grader_kind
    baseline_trial_id = record(
        comparison.baseline,
        baseline_grade.passed,
        baseline_grade.reason,
        "baseline",
    )
    candidate_trial_id = record(
        comparison.candidate,
        candidate_grade.passed,
        candidate_grade.reason,
        "candidate",
    )
    return store.record_replay_evaluation(
        candidate_id,
        case_id=case_id,
        baseline_trial_id=baseline_trial_id,
        candidate_trial_id=candidate_trial_id,
        workspace=workspace,
        user_id=user_id,
        project_id=project_id,
        model_id=candidate_request.model_id,
        parameters={"max_iterations": candidate_request.max_iterations},
    )
