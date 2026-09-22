"""Server-owned entry points for creating three-profile improvement cases."""

# The recorder intentionally calls private store validators so the public
# mutation surface cannot bypass their server-fact checks.  Keep this module's
# Pyright policy explicit rather than weakening the store implementation.
# pyright: reportPrivateUsage=false

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from .skill_improvement import (
    _RUN_ID_RE,
    SkillImprovementError,
    SkillImprovementStore,
    _bounded_text,
    _refs,
    _traceable_ref,
    skill_improvement_target,
)


def _assertion_spec(
    value: Mapping[str, object] | None,
    *,
    required_strings: tuple[str, ...] = (),
    required_bools: tuple[str, ...] = (),
) -> dict[str, object]:
    """Require server-owned facts needed by the selected deterministic grader."""

    result = dict(value or {})
    for key in required_strings:
        raw = result.get(key)
        items = (
            cast(list[object] | tuple[object, ...], raw)
            if isinstance(raw, (list, tuple))
            else ()
        )
        if not items or not all(isinstance(item, str) and item.strip() for item in items):
            raise SkillImprovementError(f"assertion_spec requires {key}")
    for key in required_bools:
        if not isinstance(result.get(key), bool):
            raise SkillImprovementError(f"assertion_spec requires {key}")
    return result


class SkillImprovementCaseRecorder:
    """Expose only trusted, profile-specific Case creation paths."""

    def __init__(self, store: SkillImprovementStore) -> None:
        self._store = store

    def record_user_confirmed_analysis_issue(
        self,
        *,
        profile_binding: str,
        origin_run_id: str,
        task_id: str,
        project_id: str,
        workspace: str | Path,
        user_id: str,
        session_id: str,
        skill_hash: str,
        summary: str,
        expected_behavior: str,
        evidence_refs: object,
        replay_snapshot_ref: str | None = None,
        assertion_spec: Mapping[str, object] | None = None,
    ) -> Any:
        """Record a current repo-analysis user correction and its Case."""
        target = skill_improvement_target("repo-analysis")
        if profile_binding != target.profile_binding:
            raise SkillImprovementError("analysis feedback profile binding is invalid")
        origin = _bounded_text(origin_run_id, field="origin_run_id", limit=128)
        refs = _refs(evidence_refs)
        if not _RUN_ID_RE.fullmatch(origin) or any(
            not _traceable_ref(item, origin_run_id=origin) for item in refs
        ):
            raise SkillImprovementError("analysis feedback evidence is not traceable")
        if not refs:
            raise SkillImprovementError("analysis feedback requires current Run evidence")
        workspace_value = str(Path(workspace).expanduser().resolve())
        if not self._store._has_repo_analysis_fact(
            origin_run_id=origin,
            project_id=project_id,
            workspace=workspace_value,
            expected_behavior=expected_behavior,
            task_id=task_id,
            session_id=session_id,
            user_id=user_id,
            evidence_refs=refs,
        ):
            raise SkillImprovementError(
                "expected behavior is not proven by the current repo-analysis Run"
            )
        if replay_snapshot_ref is None:
            raise SkillImprovementError("analysis feedback requires a task-start snapshot")
        snapshot_ref = replay_snapshot_ref
        if self._store.get_task_snapshot(
            snapshot_ref,
            workspace=workspace,
            project_id=project_id,
            session_id=session_id,
            skill_id="repo-analysis",
        ) is None:
            raise SkillImprovementError("task-start snapshot is unavailable")
        evidence = self._store.record_feedback(
            task_id=task_id,
            project_id=project_id,
            workspace=workspace,
            user_id=user_id,
            session_id=session_id,
            source_type="user_feedback",
            skill_id="repo-analysis",
            skill_hash=skill_hash,
            correction=summary,
            actual_result=expected_behavior,
            evidence_refs=tuple(dict.fromkeys((f"run-{origin}", *refs))),
        )
        assertion = _assertion_spec(
            assertion_spec
            or {
                "kind": target.grader_kind,
                "expected_paths": (expected_behavior,),
                "expected_evidence_refs": refs,
            },
            required_strings=("expected_paths", "expected_evidence_refs"),
        )
        return self._store.create_improvement_case(
            skill_id="repo-analysis",
            profile_binding=profile_binding,
            origin_run_id=origin,
            task_id=task_id,
            project_id=project_id,
            workspace=workspace,
            user_id=user_id,
            session_id=session_id,
            base_skill_hash=skill_hash,
            trigger_type="user_confirmed_missing_fact",
            trigger_summary=summary,
            expected_behavior=expected_behavior,
            evidence_refs=(evidence.evidence_id, f"run-{origin}", *refs),
            replay_snapshot_ref=snapshot_ref,
            assertion_spec=assertion,
        )

    def record_trusted_verification_failure(
        self,
        *,
        skill_id: str,
        profile_binding: str,
        origin_run_id: str,
        change_set_id: str,
        task_id: str,
        project_id: str,
        workspace: str | Path,
        user_id: str,
        session_id: str,
        skill_hash: str,
        summary: str,
        expected_behavior: str,
        failure_evidence_refs: object = (),
        replay_snapshot_ref: str | None = None,
        assertion_spec: Mapping[str, object] | None = None,
    ) -> Any:
        """Record a failure only from a server-owned ChangeSet verification fact."""
        target = skill_improvement_target(skill_id)
        if skill_id not in {"feature-delivery", "bug-fix"}:
            raise SkillImprovementError("trusted verification is not allowed for this Skill")
        if profile_binding != target.profile_binding:
            raise SkillImprovementError("verification failure profile binding is invalid")
        origin = _bounded_text(origin_run_id, field="origin_run_id", limit=128)
        change_set = _bounded_text(change_set_id, field="change_set_id", limit=128)
        verification_ref = f"verification-{origin}:{change_set}"
        failure_refs = _refs(failure_evidence_refs)
        if skill_id == "bug-fix":
            if not failure_refs:
                raise SkillImprovementError("bug-fix verification requires failure evidence")
            if not all(
                self._store._has_trusted_failure_fact(
                    failure_ref=ref,
                    project_id=project_id,
                    expected_behavior=expected_behavior,
                    origin_run_id=origin,
                    task_id=task_id,
                    session_id=session_id,
                    user_id=user_id,
                    workspace=str(Path(workspace).expanduser().resolve()),
                )
                for ref in failure_refs
            ):
                raise SkillImprovementError("bug-fix failure evidence is not server-trusted")
        verification = self._store.record_feedback(
            task_id=task_id,
            project_id=project_id,
            workspace=workspace,
            user_id=user_id,
            session_id=session_id,
            source_type="trusted_verification",
            skill_id=skill_id,
            skill_hash=skill_hash,
            correction=summary,
            actual_result=expected_behavior,
            evidence_refs=(f"run-{origin}", verification_ref),
        )
        evidence_ids: list[str] = [verification.evidence_id]
        if skill_id == "bug-fix":
            failure = self._store.record_feedback(
                task_id=task_id,
                project_id=project_id,
                workspace=workspace,
                user_id=user_id,
                session_id=session_id,
                source_type="trusted_failure_evidence",
                skill_id=skill_id,
                skill_hash=skill_hash,
                correction=summary,
                actual_result=expected_behavior,
                evidence_refs=tuple(dict.fromkeys((f"run-{origin}", *failure_refs))),
            )
            evidence_ids.append(failure.evidence_id)
        if replay_snapshot_ref is None:
            raise SkillImprovementError("verification failure requires a task-start snapshot")
        snapshot_ref = replay_snapshot_ref
        if self._store.get_task_snapshot(
            snapshot_ref,
            workspace=workspace,
            project_id=project_id,
            session_id=session_id,
            skill_id=skill_id,
        ) is None:
            raise SkillImprovementError("task-start snapshot is unavailable")
        required_bools = (
            "baseline_verification_passed",
            "candidate_verification_passed",
            "baseline_failure_repaired",
            "candidate_failure_repaired",
        )
        assertion = _assertion_spec(
            assertion_spec
            or {
                "kind": target.grader_kind,
                "baseline_verification_passed": False,
                "candidate_verification_passed": True,
                "baseline_failure_repaired": False,
                "candidate_failure_repaired": skill_id == "bug-fix",
            },
            required_bools=required_bools,
        )
        return self._store.create_improvement_case(
            skill_id=skill_id,
            profile_binding=profile_binding,
            origin_run_id=origin,
            task_id=task_id,
            project_id=project_id,
            workspace=workspace,
            user_id=user_id,
            session_id=session_id,
            base_skill_hash=skill_hash,
            trigger_type="trusted_verification_failed",
            trigger_summary=summary,
            expected_behavior=expected_behavior,
            evidence_refs=(f"run-{origin}", *evidence_ids),
            replay_snapshot_ref=snapshot_ref,
            assertion_spec=assertion,
        )
