import sqlite3
from pathlib import Path

import pytest

from devpilot.skill_improvement import (
    SkillImprovementError,
    SkillImprovementStore,
    content_sha256,
    scheduled_batch_id,
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


def _store(tmp_path: Path) -> tuple[SkillImprovementStore, Path, str, str]:
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
    evidence = store.record_feedback(
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
    return store, workspace, baseline, evidence.evidence_id


def _configure(store: SkillImprovementStore, workspace: Path) -> str:
    batch_id = scheduled_batch_id(workspace, "project-1", "user-1")
    store.configure_scheduled_batch(
        batch_id=batch_id,
        job_id="cron-1",
        workspace=workspace,
        project_id="project-1",
        user_id="user-1",
        session_key="websocket:chat-1",
        enabled=True,
    )
    return batch_id


def test_scheduled_batch_is_default_off_and_claims_only_new_verified_evidence(
    tmp_path: Path,
) -> None:
    store, workspace, baseline, evidence_id = _store(tmp_path)
    batch_id = scheduled_batch_id(workspace, "project-1", "user-1")
    assert store.get_scheduled_batch(batch_id) is None

    _configure(store, workspace)
    claim = store.claim_scheduled_batch("cron-1", lease_owner="owner-1")
    assert claim is not None
    assert claim.status == "claimed"
    assert [item.evidence_id for item in claim.evidence] == [evidence_id]
    assert claim.batch.status == "running"
    assert claim.deadline_at is not None
    assert baseline

    busy = store.claim_scheduled_batch("cron-1", lease_owner="owner-2")
    assert busy is not None
    assert busy.status == "busy"


def test_failed_batch_keeps_evidence_for_the_next_scheduled_retry(tmp_path: Path) -> None:
    store, workspace, _baseline_text, _evidence_id = _store(tmp_path)
    batch_id = _configure(store, workspace)
    claim = store.claim_scheduled_batch("cron-1", lease_owner="owner-1")
    assert claim is not None and claim.status == "claimed"

    failed = store.finish_scheduled_batch(
        batch_id,
        lease_owner="owner-1",
        error="model unavailable",
    )
    assert failed.status == "failed"
    assert failed.cursor_at is None
    assert failed.processed_evidence_ids == ()

    retry = store.claim_scheduled_batch("cron-1", lease_owner="owner-2")
    assert retry is not None and retry.status == "claimed"


def test_successful_candidate_advances_cursor_and_deduplicates_after_restart(
    tmp_path: Path,
) -> None:
    store, workspace, baseline, evidence_id = _store(tmp_path)
    batch_id = _configure(store, workspace)
    claim = store.claim_scheduled_batch("cron-1", lease_owner="owner-1")
    assert claim is not None and claim.status == "claimed"

    assert (
        store.reserve_scheduled_candidate_attempt(
            batch_id,
            lease_owner="owner-1",
            estimated_tokens=100,
        )
        == 1
    )
    candidate = store.create_candidate(
        user_id="user-1",
        project_id="project-1",
        skill_path="skills/bug-fix/SKILL.md",
        baseline_content=baseline,
        candidate_content=baseline + "\n- Re-read the current file before proposing a patch.\n",
        evidence_ids=[evidence_id],
        reason="The failed task did not re-check the source file.",
        expected_behavior="The proposal cites fresh file evidence.",
        test_set_version="p8-02-v1",
        batch_id=batch_id,
        workspace=workspace,
    )
    store.register_scheduled_candidate(
        batch_id,
        lease_owner="owner-1",
        candidate_id=candidate.candidate_id,
    )
    completed = store.finish_scheduled_batch(batch_id, lease_owner="owner-1")
    assert completed.status == "candidate_created"
    assert completed.processed_evidence_ids == (evidence_id,)
    assert completed.last_candidate_ids == (candidate.candidate_id,)

    restarted = SkillImprovementStore(store.root, workspace=workspace)
    no_new = restarted.claim_scheduled_batch("cron-1", lease_owner="owner-2")
    assert no_new is not None
    assert no_new.status == "no_new_evidence"


def test_scheduled_candidate_attempt_and_token_budgets_are_hard_limits(tmp_path: Path) -> None:
    store, workspace, _baseline_text, _evidence_id = _store(tmp_path)
    batch_id = _configure(store, workspace)
    claim = store.claim_scheduled_batch("cron-1", lease_owner="owner-1")
    assert claim is not None and claim.status == "claimed"

    store.reserve_scheduled_candidate_attempt(
        batch_id,
        lease_owner="owner-1",
        estimated_tokens=6_000,
    )
    store.reserve_scheduled_candidate_attempt(
        batch_id,
        lease_owner="owner-1",
        estimated_tokens=6_000,
    )
    with pytest.raises(SkillImprovementError, match="model-call budget"):
        store.reserve_scheduled_candidate_attempt(
            batch_id,
            lease_owner="owner-1",
            estimated_tokens=1,
        )


def test_disabling_batch_clears_lease_and_prevents_future_claims(tmp_path: Path) -> None:
    store, workspace, _baseline_text, _evidence_id = _store(tmp_path)
    batch_id = _configure(store, workspace)
    claim = store.claim_scheduled_batch("cron-1", lease_owner="owner-1")
    assert claim is not None and claim.status == "claimed"

    disabled = store.set_scheduled_batch_enabled(batch_id, enabled=False)
    assert disabled.enabled is False
    assert disabled.status == "disabled"
    assert disabled.lease_owner is None
    assert disabled.lease_expires_at is None
    next_claim = store.claim_scheduled_batch("cron-1", lease_owner="owner-2")
    assert next_claim is not None and next_claim.status == "disabled"
