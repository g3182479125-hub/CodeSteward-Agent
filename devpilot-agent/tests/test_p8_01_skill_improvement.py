import sqlite3
from pathlib import Path

import pytest

from devpilot.skill_improvement import (
    SkillImprovementError,
    SkillImprovementStore,
    content_sha256,
)


def _baseline() -> str:
    return "\n".join([
        "---",
        "name: bug-fix",
        "description: bounded fix",
        "metadata:",
        "  devpilot:",
        "    status: active",
        "    reviewed: true",
        "---",
        "",
        "# Bug fix",
        "",
        "## 执行步骤",
        "",
        "1. Read the failure evidence.",
        "",
    ])


def _store(tmp_path: Path) -> tuple[SkillImprovementStore, str]:
    workspace = tmp_path / "workspace"
    skill_path = workspace / "skills" / "bug-fix" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(_baseline(), encoding="utf-8")
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
    store = SkillImprovementStore(data_dir, workspace=workspace)
    feedback = store.record_feedback(
        task_id="task-1",
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
        source_type="user_feedback",
        skill_id="bug-fix",
        skill_hash=content_sha256(_baseline()),
        correction="先验证失败证据，再提出修改。",
        actual_result="用户确认原流程漏掉了失败证据校验。",
        evidence_refs=["run-1"],
    )
    return store, feedback.evidence_id


def test_feedback_is_redacted_and_candidate_stays_outside_skill_loader(tmp_path: Path) -> None:
    store, evidence_id = _store(tmp_path)
    feedback = store.get_feedback(evidence_id)
    assert feedback is not None
    assert feedback.processing_state == "verified"
    assert feedback.workspace == str((tmp_path / "workspace").resolve())
    assert feedback.user_id == "user-1"
    assert feedback.session_id == "session-1"

    candidate = store.create_candidate(
        user_id="user-1",
        project_id="project-1",
        skill_path="skills/bug-fix/SKILL.md",
        baseline_content=_baseline(),
        candidate_content=_baseline() + "\n- Re-read the current file before proposing a patch.\n",
        evidence_ids=[evidence_id],
        reason="The failed task did not re-check the source file.",
        expected_behavior="The proposal cites fresh file evidence.",
        test_set_version="p8-01-v1",
        workspace=tmp_path / "workspace",
    )
    assert candidate.status == "pending_evaluation"
    assert candidate.candidate_hash != candidate.base_version_hash
    assert candidate.candidate_content
    assert not (tmp_path / "data" / "skills" / "bug-fix" / "SKILL.md").exists()


def test_private_storage_cannot_live_inside_workspace(tmp_path: Path) -> None:
    with pytest.raises(SkillImprovementError, match="inside the workspace"):
        SkillImprovementStore(
            tmp_path / "workspace" / "private-data",
            workspace=tmp_path / "workspace",
        )


def test_production_store_requires_alembic_head(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(SkillImprovementError, match="initialized by Alembic"):
        SkillImprovementStore(tmp_path / "data", workspace=workspace)

    data_dir = tmp_path / "head-without-table"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "devpilot.db") as connection:
        connection.execute("create table alembic_version (version_num varchar(32) not null)")
        connection.execute(
            "insert into alembic_version (version_num) values (?)",
            ("0011_p8_skill_improvement_records",),
        )
    with pytest.raises(SkillImprovementError, match="schema is incomplete"):
        SkillImprovementStore(data_dir, workspace=workspace)


def test_production_store_accepts_the_current_linear_harness_head(tmp_path: Path) -> None:
    """P8 can open the shared database after later additive Harness migrations."""
    store, evidence_id = _store(tmp_path)
    del evidence_id
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "update alembic_version set version_num = ?",
            ("0016_h9_harness_selection_records",),
        )

    reopened = SkillImprovementStore(store.root, workspace=tmp_path / "workspace")
    snapshot = reopened.capture_task_snapshot(
        task_id="task-current-head",
        workspace=tmp_path / "workspace",
        session_id="session-current-head",
        project_id="project-current-head",
    )
    assert snapshot.content_hash == content_sha256(_baseline())


def test_integrity_key_is_independent_from_business_database(tmp_path: Path) -> None:
    store, _evidence_id = _store(tmp_path)
    assert store.integrity_key_file != store.database_path
    assert store.integrity_key_file.exists()
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "select 1 from skill_improvement_records where record_key = 'integrity:key'"
        ).fetchone() is None


def test_model_inference_requires_independent_verification(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
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
    with pytest.raises(SkillImprovementError, match="two independent"):
        store.record_feedback(
            task_id="task-1",
            project_id="project-1",
            workspace=workspace,
            user_id="user-1",
            session_id="session-1",
            source_type="model_inference",
            skill_id="bug-fix",
            skill_hash="a" * 64,
            correction="inference",
            actual_result="unverified",
            evidence_refs=["one"],
        )


def test_candidate_user_binding_is_checked_by_the_store(tmp_path: Path) -> None:
    store, evidence_id = _store(tmp_path)
    with pytest.raises(SkillImprovementError, match="requester"):
        store.create_candidate(
            user_id="another-user",
            project_id="project-1",
            skill_path="skills/bug-fix/SKILL.md",
            baseline_content=_baseline(),
            candidate_content=_baseline() + "\n- Extra check.\n",
            evidence_ids=[evidence_id],
            reason="test",
            expected_behavior="test",
            test_set_version="p8-02-v1",
            workspace=tmp_path / "workspace",
        )


def test_candidate_cannot_change_frontmatter_or_include_secret(tmp_path: Path) -> None:
    store, evidence_id = _store(tmp_path)
    with pytest.raises(SkillImprovementError, match="protected metadata"):
        store.create_candidate(
            user_id="user-1",
            project_id="project-1",
            skill_path="skills/bug-fix/SKILL.md",
            baseline_content=_baseline(),
            candidate_content=_baseline().replace("status: active", "status: enabled"),
            evidence_ids=[evidence_id],
            reason="reason",
            expected_behavior="behavior",
            test_set_version="p8-01-v1",
            workspace=tmp_path / "workspace",
        )

    with pytest.raises(SkillImprovementError, match="sensitive"):
        store.create_candidate(
            user_id="user-1",
            project_id="project-1",
            skill_path="skills/bug-fix/SKILL.md",
            baseline_content=_baseline(),
            candidate_content=_baseline() + "\napi_key=sk-abcdefghijkl\n",
            evidence_ids=[evidence_id],
            reason="reason",
            expected_behavior="behavior",
            test_set_version="p8-01-v1",
            workspace=tmp_path / "workspace",
        )


def test_candidate_batch_is_bounded_to_three(tmp_path: Path) -> None:
    store, evidence_id = _store(tmp_path)
    batch = "batch-fixed"
    for index in range(3):
        store.create_candidate(
            user_id="user-1",
            project_id="project-1",
            skill_path="skills/bug-fix/SKILL.md",
            baseline_content=_baseline(),
            candidate_content=_baseline() + f"\n- Rule {index}\n",
            evidence_ids=[evidence_id],
            reason="reason",
            expected_behavior="behavior",
            test_set_version="p8-01-v1",
            batch_id=batch,
        workspace=tmp_path / "workspace",
        )
    with pytest.raises(SkillImprovementError, match="limit is three"):
        store.create_candidate(
            user_id="user-1",
            project_id="project-1",
            skill_path="skills/bug-fix/SKILL.md",
            baseline_content=_baseline(),
            candidate_content=_baseline() + "\n- Rule 4\n",
            evidence_ids=[evidence_id],
            reason="reason",
            expected_behavior="behavior",
            test_set_version="p8-01-v1",
            batch_id=batch,
        workspace=tmp_path / "workspace",
        )
