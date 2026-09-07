from pathlib import Path

import pytest

from devpilot.skill_improvement import (
    EVALUATION_CATEGORIES,
    EVALUATION_REPEATS,
    EVALUATION_VERSIONS,
    SkillImprovementError,
    SkillImprovementStore,
    content_sha256,
)


def _baseline() -> str:
    return """---
name: bug-fix
description: bounded fix
metadata:
  devpilot:
    status: active
    reviewed: true
---

# Bug fix

## 执行步骤

1. Read the failure evidence.
"""


def _setup(tmp_path: Path, *, improvement: bool = True) -> tuple[SkillImprovementStore, Path, str]:
    workspace = tmp_path / "workspace"
    skill_path = workspace / "skills" / "bug-fix" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    baseline = _baseline()
    skill_path.write_text(baseline, encoding="utf-8")
    store = SkillImprovementStore(tmp_path / "data", workspace=workspace)
    feedback = store.record_feedback(
        task_id="task-1",
        project_id="project-1",
        source_type="user_feedback",
        skill_id="bug-fix",
        skill_hash=content_sha256(baseline),
        correction="先重新读取当前文件。",
        actual_result="原任务漏掉了文件复核。",
        evidence_refs=["run-1"],
    )
    addition = (
        "\n- Re-read the current file before proposing a patch.\n"
        if improvement
        else "\n- Keep the same steps.\n"
    )
    content = baseline + addition
    candidate = store.create_candidate(
        project_id="project-1",
        skill_path="skills/bug-fix/SKILL.md",
        baseline_content=baseline,
        candidate_content=content,
        evidence_ids=[feedback.evidence_id],
        reason="The failed task did not re-check the source file.",
        expected_behavior="The proposal cites fresh file evidence.",
        test_set_version="p8-02-v1",
        workspace=workspace,
    )
    return store, workspace, candidate.candidate_id


def test_fixed_comparison_has_18_cases_and_real_improvement(tmp_path: Path) -> None:
    store, workspace, candidate_id = _setup(tmp_path)
    report = store.evaluate_candidate(candidate_id, workspace=workspace)

    expected_cases = len(EVALUATION_CATEGORIES) * len(EVALUATION_VERSIONS) * EVALUATION_REPEATS
    assert len(report.cases) == expected_cases == 18
    assert report.baseline_successes == 6
    assert report.candidate_successes == 9
    assert report.eligible is True
    assert store.get_candidate(candidate_id).status == "pending_confirmation"


def test_confirmation_is_bound_one_time_and_restore_checks_conflict(tmp_path: Path) -> None:
    store, workspace, candidate_id = _setup(tmp_path)
    store.evaluate_candidate(candidate_id, workspace=workspace)
    old_task = store.capture_task_snapshot(
        task_id="task-old", workspace=workspace, session_id="session-1", project_id="project-1"
    )
    confirmation = store.issue_confirmation(
        candidate_id,
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
    assert active.content_hash != old_task.content_hash
    assert store.capture_task_snapshot(
        task_id="task-old", workspace=workspace, session_id="session-1", project_id="project-1"
    ).content_hash == old_task.content_hash
    assert store.capture_task_snapshot(
        task_id="task-new", workspace=workspace, session_id="session-2", project_id="project-1"
    ).content_hash == active.content_hash

    with pytest.raises(SkillImprovementError, match="already consumed"):
        store.activate_candidate(
            confirmation.confirmation_id,
            workspace=workspace,
            user_id="user-1",
            session_id="session-1",
            project_id="project-1",
        )

    skill_path = workspace / "skills" / "bug-fix" / "SKILL.md"
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8") + "\nmanual edit\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillImprovementError, match="externally modified"):
        store.restore_candidate(candidate_id, workspace=workspace)

    skill_path.write_text(active.content, encoding="utf-8")
    restored = store.restore_candidate(candidate_id, workspace=workspace)
    assert restored.content_hash == old_task.content_hash


def test_no_improvement_is_not_eligible(tmp_path: Path) -> None:
    store, workspace, candidate_id = _setup(tmp_path, improvement=False)
    report = store.evaluate_candidate(candidate_id, workspace=workspace)
    assert report.eligible is False
    assert report.status == "no_verified_improvement"
    assert store.get_candidate(candidate_id).status == "evaluation_failed"
