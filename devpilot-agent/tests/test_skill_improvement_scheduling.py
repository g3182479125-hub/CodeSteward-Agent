import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from devpilot.replay_workspace import GitReplaySnapshot
from devpilot.skill_improvement import (
    SKILL_IMPROVEMENT_TARGETS,
    SkillImprovementStore,
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


def _replayable_snapshots(store: SkillImprovementStore, workspace: Path, monkeypatch) -> None:
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


def _create_case(store: SkillImprovementStore, workspace: Path, skill_id: str, suffix: str):
    origin = f"run-{skill_id}-{suffix}"
    task_id = f"task-{skill_id}-{suffix}"
    session_id = f"session-{skill_id}-{suffix}"
    skill_hash = store.read_effective_snapshot(workspace, skill_id=skill_id).content_hash
    if skill_id == "repo-analysis":
        source_type = "user_feedback"
        refs = (origin,)
        trigger = "user_confirmed_missing_fact"
        profile = "repo-analysis@1"
    else:
        source_type = "trusted_verification"
        refs = (f"verification-{origin}:changeset-{suffix}", origin)
        trigger = "trusted_verification_failed"
        profile = f"{skill_id}@{'2' if skill_id == 'feature-delivery' else '1'}"
    evidence = store.record_feedback(
        task_id=task_id,
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id=session_id,
        source_type=source_type,
        skill_id=skill_id,
        skill_hash=skill_hash,
        correction=f"correction-{suffix}",
        actual_result=f"actual-{suffix}",
        evidence_refs=refs,
    )
    extra_evidence = None
    if skill_id == "bug-fix":
        extra_evidence = store.record_feedback(
            task_id=task_id,
            project_id="project-1",
            workspace=workspace,
            user_id="user-1",
            session_id=session_id,
            source_type="trusted_failure_evidence",
            skill_id=skill_id,
            skill_hash=skill_hash,
            correction=f"failure-{suffix}",
            actual_result=f"failure-result-{suffix}",
            evidence_refs=(f"failure-{suffix}", origin),
        )
    store.capture_task_snapshot(
        task_id=task_id,
        workspace=workspace,
        session_id=session_id,
        project_id="project-1",
        skill_id=skill_id,
        allowed_file_scope=["src/main.py"],
        verification_profile="python-syntax",
    )
    return store.create_improvement_case(
        skill_id=skill_id,
        profile_binding=profile,
        origin_run_id=origin,
        task_id=task_id,
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id=session_id,
        base_skill_hash=skill_hash,
        trigger_type=trigger,
        trigger_summary=f"summary-{suffix}",
        expected_behavior=f"expected-{suffix}",
        evidence_refs=(
            evidence.evidence_id,
            *( (extra_evidence.evidence_id,) if extra_evidence is not None else () ),
            *refs,
        ),
        replay_snapshot_ref=task_snapshot_ref(task_id),
        assertion_spec={"kind": "test"},
    )


def test_new_batch_claims_one_eligible_case_from_three_profile_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace = _store(tmp_path)
    _replayable_snapshots(store, workspace, monkeypatch)
    monkeypatch.setattr(store, "_has_trusted_verification_fact", lambda **kwargs: True)
    monkeypatch.setattr(store, "_has_trusted_failure_fact", lambda **kwargs: True)

    cases = [
        _create_case(store, workspace, "repo-analysis", "one"),
        _create_case(store, workspace, "feature-delivery", "two"),
        _create_case(store, workspace, "bug-fix", "three"),
    ]
    batch = store.configure_scheduled_batch(
        batch_id="batch-11111111-1111-1111-1111-111111111111",
        job_id="cron-three-profile",
        workspace=workspace,
        project_id="project-1",
        user_id="user-1",
        session_key="websocket:chat",
        target_skill_ids=tuple(SKILL_IMPROVEMENT_TARGETS),
    )

    assert batch.target_skill_ids == tuple(SKILL_IMPROVEMENT_TARGETS)
    claim = store.claim_scheduled_batch("cron-three-profile", lease_owner="owner-1")

    assert claim is not None
    assert claim.status == "claimed"
    assert claim.case is not None
    assert claim.case.case_id == cases[2].case_id
    assert claim.case.skill_id == "bug-fix"
    assert claim.batch.claimed_case_ids == (cases[2].case_id,)
    assert len(claim.evidence) == 2
    assert store.get_improvement_case(cases[2].case_id).status == "claimed"


def test_new_batch_does_not_claim_profiles_before_si06_candidate_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace = _store(tmp_path)
    _replayable_snapshots(store, workspace, monkeypatch)
    monkeypatch.setattr(store, "_has_trusted_verification_fact", lambda **kwargs: True)
    monkeypatch.setattr(store, "_has_trusted_failure_fact", lambda **kwargs: True)
    _create_case(store, workspace, "repo-analysis", "unsupported-repo")
    _create_case(store, workspace, "feature-delivery", "unsupported-feature")
    store.configure_scheduled_batch(
        batch_id="batch-66666666-6666-6666-6666-666666666666",
        job_id="cron-si06-gate",
        workspace=workspace,
        project_id="project-1",
        user_id="user-1",
        session_key="websocket:chat",
        target_skill_ids=tuple(SKILL_IMPROVEMENT_TARGETS),
    )

    claim = store.claim_scheduled_batch("cron-si06-gate", lease_owner="owner-1")

    assert claim is not None
    assert claim.status == "no_new_evidence"
    assert claim.case is None
    assert all(
        case.status == "eligible"
        for case in store.list_improvement_cases(project_id="project-1")
    )


def test_new_batch_has_no_model_claim_when_only_legacy_feedback_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace = _store(tmp_path)
    _replayable_snapshots(store, workspace, monkeypatch)
    skill_hash = store.read_effective_snapshot(workspace, skill_id="bug-fix").content_hash
    store.record_feedback(
        task_id="legacy-task",
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
        source_type="user_feedback",
        skill_id="bug-fix",
        skill_hash=skill_hash,
        correction="legacy feedback",
        actual_result="legacy result",
        evidence_refs=("run-legacy",),
    )
    store.configure_scheduled_batch(
        batch_id="batch-22222222-2222-2222-2222-222222222222",
        job_id="cron-new-no-case",
        workspace=workspace,
        project_id="project-1",
        user_id="user-1",
        session_key="websocket:chat",
        target_skill_ids=tuple(SKILL_IMPROVEMENT_TARGETS),
    )

    claim = store.claim_scheduled_batch("cron-new-no-case", lease_owner="owner-1")

    assert claim is not None
    assert claim.status == "no_new_evidence"
    assert claim.evidence == ()
    assert claim.case is None


def test_legacy_batch_still_claims_verified_bug_fix_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace = _store(tmp_path)
    _replayable_snapshots(store, workspace, monkeypatch)
    skill_hash = store.read_effective_snapshot(workspace, skill_id="bug-fix").content_hash
    evidence = store.record_feedback(
        task_id="legacy-task",
        project_id="project-1",
        workspace=workspace,
        user_id="user-1",
        session_id="session-1",
        source_type="user_feedback",
        skill_id="bug-fix",
        skill_hash=skill_hash,
        correction="legacy feedback",
        actual_result="legacy result",
        evidence_refs=("run-legacy",),
    )
    store.configure_scheduled_batch(
        batch_id="batch-33333333-3333-3333-3333-333333333333",
        job_id="cron-legacy",
        workspace=workspace,
        project_id="project-1",
        user_id="user-1",
        session_key="websocket:chat",
    )

    claim = store.claim_scheduled_batch("cron-legacy", lease_owner="owner-1")

    assert claim is not None
    assert claim.status == "claimed"
    assert claim.case is None
    assert claim.evidence[0].evidence_id == evidence.evidence_id
    assert claim.batch.target_skill_ids == ("bug-fix",)


def test_cancelled_case_claim_is_released_without_marking_it_processed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace = _store(tmp_path)
    _replayable_snapshots(store, workspace, monkeypatch)
    monkeypatch.setattr(store, "_has_trusted_verification_fact", lambda **kwargs: True)
    monkeypatch.setattr(store, "_has_trusted_failure_fact", lambda **kwargs: True)
    case = _create_case(store, workspace, "bug-fix", "cancel")
    store.configure_scheduled_batch(
        batch_id="batch-44444444-4444-4444-4444-444444444444",
        job_id="cron-cancel",
        workspace=workspace,
        project_id="project-1",
        user_id="user-1",
        session_key="websocket:chat",
        target_skill_ids=tuple(SKILL_IMPROVEMENT_TARGETS),
    )

    claim = store.claim_scheduled_batch("cron-cancel", lease_owner="owner-1")
    assert claim is not None and claim.case is not None
    persisted = store.get_scheduled_batch("batch-44444444-4444-4444-4444-444444444444")
    assert persisted is not None
    assert (persisted.status, persisted.lease_owner) == ("running", "owner-1")
    cancelled = store.finish_scheduled_batch(
        "batch-44444444-4444-4444-4444-444444444444",
        lease_owner="owner-1",
        cancelled=True,
    )

    released = store.get_improvement_case(case.case_id)
    assert cancelled.status == "idle"
    assert cancelled.processed_case_ids == ()
    assert released is not None
    assert released.status == "eligible"
    assert released.selected_batch_id is None


def test_failed_case_claim_is_released_for_bounded_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace = _store(tmp_path)
    _replayable_snapshots(store, workspace, monkeypatch)
    monkeypatch.setattr(store, "_has_trusted_verification_fact", lambda **kwargs: True)
    monkeypatch.setattr(store, "_has_trusted_failure_fact", lambda **kwargs: True)
    case = _create_case(store, workspace, "bug-fix", "failed")
    store.configure_scheduled_batch(
        batch_id="batch-77777777-7777-7777-7777-777777777777",
        job_id="cron-failed",
        workspace=workspace,
        project_id="project-1",
        user_id="user-1",
        session_key="websocket:chat",
        target_skill_ids=tuple(SKILL_IMPROVEMENT_TARGETS),
    )

    first = store.claim_scheduled_batch("cron-failed", lease_owner="owner-1")
    assert first is not None and first.case is not None
    store.reserve_scheduled_candidate_attempt(
        "batch-77777777-7777-7777-7777-777777777777",
        lease_owner="owner-1",
        estimated_tokens=100,
    )
    failed = store.finish_scheduled_batch(
        "batch-77777777-7777-7777-7777-777777777777",
        lease_owner="owner-1",
        error="provider failed",
    )
    released = store.get_improvement_case(case.case_id)

    assert failed.status == "failed"
    assert released is not None
    assert released.status == "eligible"
    assert released.selected_batch_id is None
    retry = store.claim_scheduled_batch("cron-failed", lease_owner="owner-2")
    assert retry is not None and retry.status == "claimed"
    assert retry.case is not None and retry.case.case_id == case.case_id
    store.reserve_scheduled_candidate_attempt(
        "batch-77777777-7777-7777-7777-777777777777",
        lease_owner="owner-2",
        estimated_tokens=100,
    )
    store.finish_scheduled_batch(
        "batch-77777777-7777-7777-7777-777777777777",
        lease_owner="owner-2",
        error="provider failed again",
    )
    exhausted = store.claim_scheduled_batch("cron-failed", lease_owner="owner-3")
    assert exhausted is not None and exhausted.status == "budget_exhausted"


def test_expired_lease_recovers_case_after_store_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace = _store(tmp_path)
    _replayable_snapshots(store, workspace, monkeypatch)
    monkeypatch.setattr(store, "_has_trusted_verification_fact", lambda **kwargs: True)
    monkeypatch.setattr(store, "_has_trusted_failure_fact", lambda **kwargs: True)
    case = _create_case(store, workspace, "bug-fix", "restart")
    batch = store.configure_scheduled_batch(
        batch_id="batch-88888888-8888-8888-8888-888888888888",
        job_id="cron-restart",
        workspace=workspace,
        project_id="project-1",
        user_id="user-1",
        session_key="websocket:chat",
        target_skill_ids=tuple(SKILL_IMPROVEMENT_TARGETS),
    )
    first = store.claim_scheduled_batch("cron-restart", lease_owner="owner-1")
    assert first is not None and first.case is not None
    expired = replace(first.batch, lease_expires_at=0.0)
    store._atomic_write(store.batches_dir / f"{batch.batch_id}.json", expired.to_dict())
    restarted = SkillImprovementStore(store.root, workspace=workspace)

    recovered = restarted.claim_scheduled_batch("cron-restart", lease_owner="owner-2")

    assert recovered is not None and recovered.status == "claimed"
    assert recovered.case is not None and recovered.case.case_id == case.case_id
    assert recovered.batch.lease_owner == "owner-2"


def test_concurrent_claims_bind_one_case_to_one_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace = _store(tmp_path)
    _replayable_snapshots(store, workspace, monkeypatch)
    monkeypatch.setattr(store, "_has_trusted_verification_fact", lambda **kwargs: True)
    monkeypatch.setattr(store, "_has_trusted_failure_fact", lambda **kwargs: True)
    case = _create_case(store, workspace, "bug-fix", "concurrent")
    store.configure_scheduled_batch(
        batch_id="batch-99999999-9999-9999-9999-999999999999",
        job_id="cron-concurrent",
        workspace=workspace,
        project_id="project-1",
        user_id="user-1",
        session_key="websocket:chat",
        target_skill_ids=tuple(SKILL_IMPROVEMENT_TARGETS),
    )
    stores = [SkillImprovementStore(store.root, workspace=workspace) for _ in range(2)]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: args[0].claim_scheduled_batch(
                    "cron-concurrent", lease_owner=args[1]
                ),
                zip(stores, ("owner-a", "owner-b"), strict=True),
            )
        )

    statuses = sorted(result.status for result in results if result is not None)
    assert statuses == ["busy", "claimed"]
    claimed = next(
        result for result in results if result is not None and result.status == "claimed"
    )
    assert claimed.case is not None and claimed.case.case_id == case.case_id
    assert store.get_improvement_case(case.case_id).selected_batch_id == claimed.batch.batch_id


def test_completed_case_advances_profile_rotation_before_next_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, workspace = _store(tmp_path)
    _replayable_snapshots(store, workspace, monkeypatch)
    monkeypatch.setattr(store, "_has_trusted_verification_fact", lambda **kwargs: True)
    monkeypatch.setattr(store, "_has_trusted_failure_fact", lambda **kwargs: True)
    first = _create_case(store, workspace, "bug-fix", "rotation-first")
    baseline = store.read_effective_snapshot(workspace, skill_id="bug-fix")
    evidence_id = next(
        ref for ref in first.evidence_refs if store.get_feedback(ref) is not None
    )
    store.configure_scheduled_batch(
        batch_id="batch-55555555-5555-5555-5555-555555555555",
        job_id="cron-rotation",
        workspace=workspace,
        project_id="project-1",
        user_id="user-1",
        session_key="websocket:chat",
        target_skill_ids=tuple(SKILL_IMPROVEMENT_TARGETS),
    )

    first_claim = store.claim_scheduled_batch("cron-rotation", lease_owner="owner-1")
    assert first_claim is not None and first_claim.case is not None
    candidate = store.create_candidate(
        user_id="user-1",
        project_id="project-1",
        skill_path="skills/bug-fix/SKILL.md",
        baseline_content=baseline.content,
        candidate_content=baseline.content + "\n- Re-check the failure.\n",
        evidence_ids=(evidence_id,),
        case_ids=(first.case_id,),
        reason="re-check the failure",
        expected_behavior="the failure is re-checked",
        test_set_version="p8-02-v1",
        batch_id="batch-55555555-5555-5555-5555-555555555555",
        workspace=workspace,
    )
    store.register_scheduled_candidate(
        "batch-55555555-5555-5555-5555-555555555555",
        lease_owner="owner-1",
        candidate_id=candidate.candidate_id,
    )
    finished = store.finish_scheduled_batch(
        "batch-55555555-5555-5555-5555-555555555555",
        lease_owner="owner-1",
    )
    assert finished.last_selected_skill_id == "bug-fix"

    next_case = _create_case(store, workspace, "bug-fix", "rotation-next")
    second_claim = store.claim_scheduled_batch("cron-rotation", lease_owner="owner-2")

    assert second_claim is not None and second_claim.case is not None
    assert second_claim.case.case_id == next_case.case_id
    assert second_claim.case.skill_id == "bug-fix"
