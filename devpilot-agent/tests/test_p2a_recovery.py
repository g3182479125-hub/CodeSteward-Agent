from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from devpilot.domain import FileOperation
from devpilot.patches import (
    AuthorizationGrant,
    FilePatch,
    PatchError,
    StructuredPatchService,
    sha256_bytes,
)
from devpilot.paths import AllowedRoots
from devpilot.storage import ChangeSetRecord, FileChangeRecord, ProjectStore


def setup_store(tmp_path: Path) -> tuple[ProjectStore, Path, str]:
    project = tmp_path / "project"
    project.mkdir()
    store = ProjectStore(tmp_path / "data" / "devpilot.db")
    store.initialize()
    project_id = store.ensure_project(str(project.resolve()))
    return store, project, project_id


def test_plan_persists_immutable_patch_and_snapshot_material(tmp_path: Path) -> None:
    store, project, project_id = setup_store(tmp_path)
    target = project / "main.py"
    target.write_bytes(b"old\n")
    service = StructuredPatchService(allowed_roots=AllowedRoots.from_paths([tmp_path]), store=store)

    plan = service.plan(
        project,
        project_id=project_id,
        patches=(FilePatch("main.py", FileOperation.UPDATE, "new\n", sha256_bytes(b"old\n")),),
    )

    with store._session_factory() as session:  # repository verification, not production access
        record = session.get(ChangeSetRecord, plan.change_set.id)
        changes = session.scalars(
            select(FileChangeRecord).where(FileChangeRecord.change_set_id == plan.change_set.id)
        ).all()
    assert record is not None
    assert record.status == "planned"
    assert record.project_id == project_id
    assert json.loads(record.patches_json or "[]")[0]["content"] == "new\n"
    assert json.loads(record.snapshots_json or "[]")[0]["content"] == "old\n"
    assert len(changes) == 1


def test_restart_after_applying_locks_project_and_rejects_write(tmp_path: Path) -> None:
    store, project, project_id = setup_store(tmp_path)
    target = project / "main.py"
    target.write_bytes(b"old\n")
    service = StructuredPatchService(allowed_roots=AllowedRoots.from_paths([tmp_path]), store=store)
    plan = service.plan(
        project,
        project_id=project_id,
        patches=(FilePatch("main.py", FileOperation.UPDATE, "new\n", sha256_bytes(b"old\n")),),
    )
    store.update_change_set_status(plan.change_set.id, "applying")

    recovered = StructuredPatchService(
        allowed_roots=AllowedRoots.from_paths([tmp_path]), store=store
    )
    grant = AuthorizationGrant(
        change_set_id=plan.change_set.id,
        project_id=project_id,
        patch_hash=plan.patch_hash,
        scope_hash=plan.scope_hash,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    with pytest.raises(PatchError, match="PROJECT_LOCKED"):
        recovered.apply(plan, grant)
    assert store.is_project_locked(project_id) is True
    assert target.read_bytes() == b"old\n"


def test_apply_and_rollback_update_durable_lifecycle(tmp_path: Path) -> None:
    store, project, project_id = setup_store(tmp_path)
    target = project / "main.py"
    target.write_bytes(b"old\n")
    service = StructuredPatchService(
        allowed_roots=AllowedRoots.from_paths([tmp_path]), store=store
    )
    plan = service.plan(
        project,
        project_id=project_id,
        patches=(FilePatch("main.py", FileOperation.UPDATE, "new\n", sha256_bytes(b"old\n")),),
    )
    grant = AuthorizationGrant(
        change_set_id=plan.change_set.id,
        project_id=project_id,
        patch_hash=plan.patch_hash,
        scope_hash=plan.scope_hash,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    receipt = service.apply(plan, grant)
    with store._session_factory() as session:
        record = session.get(ChangeSetRecord, plan.change_set.id)
    assert receipt.change_set_id == plan.change_set.id
    assert record is not None and record.status == "applied"

    service.rollback(receipt, plan)
    with store._session_factory() as session:
        record = session.get(ChangeSetRecord, plan.change_set.id)
    assert record is not None and record.status == "reverted"
    assert target.read_bytes() == b"old\n"
