import sqlite3
from pathlib import Path

import pytest

from devpilot.replay_workspace import GitReplaySnapshot
from devpilot.skill_improvement import (
    IMPROVEMENT_CASE_STATES,
    REPLAY_TRIAL_STATUSES,
    SKILL_IMPROVEMENT_TARGETS,
    SkillImprovementError,
    SkillImprovementStore,
    content_sha256,
    task_snapshot_ref,
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
        project_trusted=True,
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
