import sqlite3
from pathlib import Path

from devpilot.skill_improvement import (
    IMPROVEMENT_CASE_STATES,
    REPLAY_TRIAL_STATUSES,
    SKILL_IMPROVEMENT_TARGETS,
    SkillImprovementStore,
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


def test_improvement_case_and_replay_trial_round_trip(tmp_path: Path) -> None:
    store, workspace = _store(tmp_path)
    skill_hash = store.read_effective_snapshot(workspace, skill_id="repo-analysis").content_hash

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
        evidence_refs=("run-analysis-1",),
        replay_snapshot_ref="snapshot-analysis-1",
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
        snapshot_ref="snapshot-analysis-1",
        started_at="2026-09-21T00:00:00+00:00",
        finished_at="2026-09-21T00:00:01+00:00",
        token_count=42,
        duration_seconds=1.0,
        tool_trace_ref="trace-analysis-1",
        final_output_ref="output-analysis-1",
        assertion_result={"passed": True},
        status="passed",
    )

    assert trial.trial_id.startswith("trial-")
    assert trial.case_id == case.case_id
    assert trial.status in REPLAY_TRIAL_STATUSES
    assert store.get_replay_trial(trial.trial_id) == trial
    assert store.list_replay_trials(case_id=case.case_id) == [trial]
    assert case.status in IMPROVEMENT_CASE_STATES
