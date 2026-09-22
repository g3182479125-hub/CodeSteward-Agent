"""Production orchestration for one SI-07 baseline/candidate comparison."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from nanobot.agent.skill_replay import (
    ReplayRequest,
    ReplayTrialResult,
    grade_replay,
    run_replay_comparison,
    skill_content_hash,
)

from .skill_improvement import EvaluationReport, SkillImprovementError, SkillImprovementStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _trial_status(result: ReplayTrialResult, passed: bool) -> str:
    if result.status != "passed":
        return result.status
    return "passed" if passed else "failed"


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        item
        for item in cast(list[object] | tuple[object, ...], value)
        if isinstance(item, str)
    )


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
    snapshot_ref = case.replay_snapshot_ref
    if snapshot_ref is None:
        raise SkillImprovementError("replay case has no snapshot")
    snapshot = store.get_task_snapshot(
        snapshot_ref,
        workspace=case.workspace,
        project_id=case.project_id,
        session_id=case.session_id,
        skill_id=case.skill_id,
    )
    if snapshot is None or not snapshot.replayable:
        raise SkillImprovementError("replay case snapshot is not usable")
    repository_root = snapshot.repository_root
    if not isinstance(repository_root, str) or not repository_root:
        raise SkillImprovementError("replay snapshot has no repository root")
    if skill_content_hash(baseline_skill) != case.base_skill_hash:
        raise SkillImprovementError("baseline Skill content does not match the Case")
    if skill_content_hash(candidate_skill) != candidate.candidate_hash:
        raise SkillImprovementError("candidate Skill content does not match the candidate")
    case_expected_behavior = case.expected_behavior
    if not case_expected_behavior.strip():
        raise SkillImprovementError("replay Case is missing expected behavior")
    profile_name = case.profile_binding.split("@", 1)[0]
    if expected_behavior is not None and expected_behavior != case_expected_behavior:
        raise SkillImprovementError("replay assertion does not match the Case")
    spec_paths = _string_tuple(case.assertion_spec.get("expected_paths", ()))
    spec_evidence = _string_tuple(
        case.assertion_spec.get("expected_evidence_refs", ())
    )
    if profile_name == "repo-analysis" and not spec_paths:
        raise SkillImprovementError("replay Case is missing expected paths")
    if profile_name == "repo-analysis" and not spec_evidence:
        raise SkillImprovementError("replay Case is missing expected evidence refs")
    if expected_paths and tuple(expected_paths) != spec_paths:
        raise SkillImprovementError("replay expected paths do not match the Case")
    if expected_evidence_refs and tuple(expected_evidence_refs) != spec_evidence:
        raise SkillImprovementError("replay evidence refs do not match the Case")
    for key, supplied in (
        ("baseline_verification_passed", baseline_verification_passed),
        ("candidate_verification_passed", candidate_verification_passed),
        ("baseline_failure_repaired", baseline_failure_repaired),
        ("candidate_failure_repaired", candidate_failure_repaired),
    ):
        asserted = case.assertion_spec.get(key)
        if profile_name in {"feature-delivery", "bug-fix"} and not isinstance(asserted, bool):
            raise SkillImprovementError(f"replay Case is missing {key}")
        if isinstance(asserted, bool) and asserted != supplied:
            raise SkillImprovementError("replay verification fact does not match the Case")
    requests = (baseline_request, candidate_request)
    for request in requests:
        if (
            request.task_id != case.task_id
            or request.project_id != case.project_id
            or request.profile_name != profile_name
            or request.skill_id != case.skill_id
            or request.snapshot_ref != snapshot_ref
            or request.snapshot_commit != snapshot.start_commit_sha
            or request.source_repository_root is None
            or request.source_repository_root.expanduser().resolve()
            != Path(repository_root).expanduser().resolve()
        ):
            raise SkillImprovementError("replay request is not bound to its Case snapshot")

    comparison = await run_replay_comparison(
        baseline_request,
        candidate_request,
        baseline_skill=baseline_skill,
        candidate_skill=candidate_skill,
    )
    baseline_grade = grade_replay(
        baseline_request,
        comparison.baseline,
        expected_behavior=case_expected_behavior,
        expected_paths=spec_paths,
        expected_evidence_refs=spec_evidence,
        trusted_verification_passed=baseline_verification_passed,
        original_failure_repaired=baseline_failure_repaired,
        allowed_file_scope=snapshot.allowed_file_scope,
    )
    candidate_grade = grade_replay(
        candidate_request,
        comparison.candidate,
        expected_behavior=case_expected_behavior,
        expected_paths=spec_paths,
        expected_evidence_refs=spec_evidence,
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
            parameters={
                "max_iterations": baseline_request.max_iterations,
                "replay_max_tokens": baseline_request.max_tokens,
                "replay_timeout_s": baseline_request.llm_timeout_s or 300,
            },
            snapshot_ref=snapshot_ref,
            started_at=_now(),
            finished_at=_now(),
            token_count=result.token_count,
            duration_seconds=result.duration_seconds,
            tool_trace_ref=f"trace-{trial_id}",
            final_output_ref=f"output-{trial_id}",
            assertion_result={
                "passed": grade_passed,
                "grader_kind": grade_kind,
                "reason": reason,
                "changed_paths": list(result.changed_paths),
                "tool_trace": [dict(event) for event in result.tool_events],
                "final_output": result.final_output,
                "error": result.error,
                "usage": dict(result.usage),
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
        parameters={
            "max_iterations": candidate_request.max_iterations,
            "replay_max_tokens": candidate_request.max_tokens,
            "replay_timeout_s": candidate_request.llm_timeout_s or 300,
        },
        budget={
            "max_model_calls": 3,
            "max_tokens": 20_000,
            "max_seconds": 600,
            "candidate_generation_max_tokens": 4_000,
            "replay_max_tokens": 8_000,
            "replay_timeout_s": 300,
        },
    )
