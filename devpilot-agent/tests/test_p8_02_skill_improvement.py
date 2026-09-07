import json
import sqlite3
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
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "devpilot.db") as connection:
        connection.execute("create table alembic_version (version_num varchar(32) not null)")
        connection.execute(
            "insert into alembic_version (version_num) values (?)",
            ("0011_p8_skill_improvement_records",),
        )
        connection.execute(
            "create table skill_improvement_records ("
            "id integer primary key autoincrement, record_key text not null unique, "
            "record_type text not null, record_id text not null, payload_json text not null, "
            "record_mac text not null, workspace text, project_id text, created_at text not null)"
        )
    store = SkillImprovementStore(data_dir, workspace=workspace)
    feedback = store.record_feedback(
        task_id="task-1",
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
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
        user_id="user-1",
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
    assert all("agent_trace" in item for item in report.cases)
    assert any("read_file" in item["agent_trace"] for item in report.cases)
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


def test_evaluation_and_confirmation_keep_workspace_and_project_binding(tmp_path: Path) -> None:
    store, workspace, candidate_id = _setup(tmp_path)
    other_workspace = tmp_path / "other-workspace"
    skill_path = other_workspace / "skills" / "bug-fix" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(_baseline(), encoding="utf-8")

    with pytest.raises(SkillImprovementError, match="workspace mismatch"):
        store.evaluate_candidate(candidate_id, workspace=other_workspace)

    store.evaluate_candidate(candidate_id, workspace=workspace)
    with pytest.raises(SkillImprovementError, match="project mismatch"):
        store.issue_confirmation(
            candidate_id,
            user_id="user-1",
            session_id="session-1",
            workspace=workspace,
            project_id="other-project",
        )


def test_candidate_is_bound_to_one_user_and_hidden_from_other_users(tmp_path: Path) -> None:
    store, workspace, candidate_id = _setup(tmp_path)
    original = store.get_candidate(candidate_id)
    assert original is not None
    assert store.get_candidate(candidate_id, workspace=workspace, user_id="user-1") is not None
    assert store.get_candidate(candidate_id, workspace=workspace, user_id="user-2") is None
    assert store.list_candidates(workspace=workspace, user_id="user-2") == []

    feedback = store.record_feedback(
        task_id="task-2",
        project_id="project-1",
        workspace=workspace,
        user_id="user-2",
        session_id="session-2",
        source_type="user_feedback",
        skill_id="bug-fix",
        skill_hash=content_sha256(_baseline()),
        correction="另一个用户的反馈。",
        actual_result="不能混入同一候选。",
        evidence_refs=["run-2"],
    )
    with pytest.raises(SkillImprovementError, match="one user"):
        store.create_candidate(
            user_id="user-1",
            project_id="project-1",
            skill_path="skills/bug-fix/SKILL.md",
            baseline_content=_baseline(),
            candidate_content=_baseline() + "\n- Keep user scope isolated.\n",
            evidence_ids=[original.evidence_ids[0], feedback.evidence_id],
            reason="mixed users",
            expected_behavior="reject mixed ownership",
            test_set_version="p8-02-v1",
            workspace=workspace,
        )


def test_deleted_feedback_invalidates_linked_candidate(tmp_path: Path) -> None:
    store, workspace, candidate_id = _setup(tmp_path)
    feedback = store.get_feedback(store.get_candidate(candidate_id).evidence_ids[0])
    assert feedback is not None
    deleted = store.delete_feedback(
        feedback.evidence_id,
        workspace=workspace,
        user_id="user-1",
        project_id="project-1",
    )
    assert deleted.processing_state == "deleted"
    candidate = store.get_candidate(candidate_id)
    assert candidate.status == "stale"
    assert candidate.candidate_content == "[deleted candidate content]"
    with sqlite3.connect(store.database_path) as connection:
        payloads = [
            str(row[0])
            for row in connection.execute(
                "select payload_json from skill_improvement_records where record_type = 'feedback'"
            )
        ]
    assert len(payloads) == 1
    assert "先重新读取当前文件" not in payloads[0]
    assert "原任务漏掉了文件复核" not in payloads[0]
    assert "run-1" not in payloads[0]
    with pytest.raises(SkillImprovementError, match="deleted"):
        store.verify_feedback(feedback.evidence_id)


def test_confirmation_expiry_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, workspace, candidate_id = _setup(tmp_path)
    store.evaluate_candidate(candidate_id, workspace=workspace)
    confirmation = store.issue_confirmation(
        candidate_id,
        user_id="user-1",
        session_id="session-1",
        workspace=workspace,
        project_id="project-1",
    )
    monkeypatch.setattr(
        "devpilot.skill_improvement.time.time", lambda: confirmation.expires_at + 1
    )
    with pytest.raises(SkillImprovementError, match="expired"):
        store.activate_candidate(
            confirmation.confirmation_id,
            workspace=workspace,
            user_id="user-1",
            session_id="session-1",
            project_id="project-1",
        )


def test_integrity_and_committed_journal_recovery(tmp_path: Path) -> None:
    store, workspace, candidate_id = _setup(tmp_path)
    store.evaluate_candidate(candidate_id, workspace=workspace)
    confirmation = store.issue_confirmation(
        candidate_id,
        user_id="user-1",
        session_id="session-1",
        workspace=workspace,
        project_id="project-1",
    )
    store.activate_candidate(
        confirmation.confirmation_id,
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
        project_id="project-1",
    )
    candidate = store.get_candidate(candidate_id)
    raw = {**candidate.to_dict(), "status": "pending_confirmation"}
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "update skill_improvement_records set payload_json = ? "
            "where record_key = ?",
            (json.dumps(raw), f"candidate:{candidate_id}"),
        )
    with pytest.raises(SkillImprovementError, match="integrity"):
        store.get_candidate(candidate_id)

    # A clean committed journal repairs a stale in-memory state on restart.
    store._atomic_write(
        store.candidates_dir / f"{candidate_id}.json",
        {**candidate.to_dict(), "status": "pending_confirmation"},
    )
    recovered = SkillImprovementStore(tmp_path / "data", workspace=workspace)
    assert recovered.get_candidate(candidate_id).status == "active"


def test_no_improvement_is_not_eligible(tmp_path: Path) -> None:
    store, workspace, candidate_id = _setup(tmp_path, improvement=False)
    report = store.evaluate_candidate(candidate_id, workspace=workspace)
    assert report.eligible is False
    assert report.status == "no_verified_improvement"
    assert store.get_candidate(candidate_id).status == "evaluation_failed"
