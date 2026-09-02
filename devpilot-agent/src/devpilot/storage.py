"""Small SQLite persistence layer for P1 business records."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    insert,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .redaction import redact_sensitive_payload


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ProjectRecord(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    real_path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunRecord(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEventRecord(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ArtifactRecord(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskRecord(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChangePlanRecord(Base):
    __tablename__ = "change_plans"
    __table_args__ = (UniqueConstraint("task_id", "version", name="uq_change_plan_task_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_summary: Mapped[str] = mapped_column(Text, nullable=False)
    check_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    stop_conditions_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChangeSetRecord(Base):
    __tablename__ = "change_sets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    plan_id: Mapped[str] = mapped_column(ForeignKey("change_plans.id"), nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"))
    project_path: Mapped[str | None] = mapped_column(Text)
    scope_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    patch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    before_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    after_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    rollback_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    patches_json: Mapped[str | None] = mapped_column(Text)
    snapshots_json: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApprovalRecord(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    change_set_id: Mapped[str] = mapped_column(ForeignKey("change_sets.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    patch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    capability: Mapped[str] = mapped_column(String(32), nullable=False)
    token_digest: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FileChangeRecord(Base):
    __tablename__ = "file_changes"
    __table_args__ = (
        UniqueConstraint("change_set_id", "relative_path", name="uq_file_change_path"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_set_id: Mapped[str] = mapped_column(ForeignKey("change_sets.id"), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(String(16), nullable=False)
    expected_sha256: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    before_size: Mapped[int] = mapped_column(Integer, nullable=False)
    after_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectLockRecord(Base):
    __tablename__ = "project_locks"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    reason: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectStore:
    """Repository-style operations; callers never build SQL strings."""

    def __init__(self, database_path: str | Path) -> None:
        path = Path(database_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{path}", future=True)
        self._session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

    def require_schema_head(self, expected: str) -> None:
        """Refuse service startup unless Alembic has recorded the required head."""
        with self.engine.connect() as connection:
            try:
                actual = connection.exec_driver_sql(
                    "select version_num from alembic_version"
                ).scalar_one_or_none()
            except Exception as exc:
                raise RuntimeError("database is not initialized by Alembic") from exc
        if actual != expected:
            raise RuntimeError(f"database schema head is {actual!r}; expected {expected!r}")

    def ensure_project(self, real_path: str) -> str:
        with self._session_factory.begin() as session:
            existing = session.query(ProjectRecord).filter_by(real_path=real_path).one_or_none()
            if existing:
                return existing.id
            project_id = str(uuid.uuid4())
            session.add(ProjectRecord(id=project_id, real_path=real_path, created_at=utc_now()))
            return project_id

    def get_project_path(self, project_id: str) -> str:
        with self._session_factory() as session:
            project = session.get(ProjectRecord, project_id)
            if project is None:
                raise KeyError("unknown project")
            return project.real_path

    def start_run(self, project_id: str, kind: str = "repo-analysis") -> str:
        run_id = str(uuid.uuid4())
        with self._session_factory.begin() as session:
            session.add(
                RunRecord(
                    id=run_id,
                    project_id=project_id,
                    kind=kind,
                    status="running",
                    started_at=utc_now(),
                )
            )
            session.add(
                RunEventRecord(
                    run_id=run_id,
                    event_type="run.started",
                    payload_json=json.dumps({"kind": kind}, ensure_ascii=False),
                    created_at=utc_now(),
                )
            )
        return run_id

    def append_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        safe_payload = redact_sensitive_payload(payload)
        with self._session_factory.begin() as session:
            session.add(
                RunEventRecord(
                    run_id=run_id,
                    event_type=event_type,
                    payload_json=json.dumps(safe_payload, ensure_ascii=False, sort_keys=True),
                    created_at=utc_now(),
                )
            )

    def validate_open_run(self, *, project_id: str, run_id: str) -> None:
        with self._session_factory() as session:
            run = session.get(RunRecord, run_id)
            if run is None or run.project_id != project_id or run.status != "running":
                raise ValueError("run does not belong to project or is not open")

    def run_belongs_to_project(self, *, project_id: str, run_id: str) -> bool:
        with self._session_factory() as session:
            run = session.get(RunRecord, run_id)
            return run is not None and run.project_id == project_id

    def get_run_status(self, run_id: str) -> str:
        with self._session_factory() as session:
            run = session.get(RunRecord, run_id)
            if run is None:
                raise KeyError(f"unknown run: {run_id}")
            return run.status

    def get_run_kind(self, run_id: str) -> str:
        with self._session_factory() as session:
            run = session.get(RunRecord, run_id)
            if run is None:
                raise KeyError(f"unknown run: {run_id}")
            return run.kind

    def finish_run(self, run_id: str, status: str, payload: dict[str, Any]) -> None:
        if status not in {"completed", "failed"}:
            raise ValueError("P1 runs can only finish as completed or failed")
        with self._session_factory.begin() as session:
            run = session.get(RunRecord, run_id)
            if run is None:
                raise ValueError(f"unknown run: {run_id}")
            if run.finished_at is not None:
                raise ValueError(f"run already finished: {run_id}")
            run.status = status
            run.finished_at = utc_now()
            session.add(
                RunEventRecord(
                    run_id=run_id,
                    event_type=f"run.{status}",
                    payload_json=json.dumps(
                        redact_sensitive_payload(payload), ensure_ascii=False, sort_keys=True
                    ),
                    created_at=utc_now(),
                )
            )

    def record_artifact(
        self,
        *,
        artifact_id: str,
        project_id: str,
        run_id: str,
        relative_path: str,
        sha256: str,
        size_bytes: int,
    ) -> None:
        with self._session_factory.begin() as session:
            session.add(
                ArtifactRecord(
                    id=artifact_id,
                    project_id=project_id,
                    run_id=run_id,
                    kind="markdown",
                    relative_path=relative_path,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    created_at=utc_now(),
                )
            )

    def count(self, model: type[Base]) -> int:
        with self._session_factory() as session:
            return int(session.query(model).count())

    def persist_patch_plan(
        self,
        *,
        change_set_id: str,
        task_id: str,
        plan_id: str,
        project_id: str,
        project_path: str,
        patch_hash: str,
        scope_hash: str,
        status: str,
        before_manifest: str,
        after_manifest: str,
        rollback_manifest: str,
        patches_json: str,
        snapshots_json: str,
        file_changes: list[dict[str, Any]],
        run_id: str | None = None,
    ) -> None:
        """Persist the immutable plan and its recovery material before any write."""
        with self._session_factory.begin() as session:
            if session.get(TaskRecord, task_id) is None:
                session.add(
                    TaskRecord(
                        id=task_id,
                        project_id=project_id,
                        kind="code-delivery",
                        request_text="structured patch plan",
                        status="planning",
                        scope_json="[]",
                        created_at=utc_now(),
                    )
                )
            if session.get(ChangePlanRecord, plan_id) is None:
                session.add(
                    ChangePlanRecord(
                        id=plan_id,
                        task_id=task_id,
                        version=1,
                        status="frozen",
                        plan_hash=patch_hash,
                        scope_hash=scope_hash,
                        risk_summary="bounded text patch; apply remains separately authorized",
                        check_ids_json="[]",
                        stop_conditions_json="[]",
                        created_at=utc_now(),
                    )
                )
            session.add(
                ChangeSetRecord(
                    id=change_set_id,
                    task_id=task_id,
                    plan_id=plan_id,
                    project_id=project_id,
                    run_id=run_id,
                    project_path=project_path,
                    scope_hash=scope_hash,
                    status=status,
                    patch_hash=patch_hash,
                    before_manifest_json=before_manifest,
                    after_manifest_json=after_manifest,
                    rollback_manifest_json=rollback_manifest,
                    patches_json=patches_json,
                    snapshots_json=snapshots_json,
                    created_at=utc_now(),
                )
            )
            for change in file_changes:
                session.add(
                    FileChangeRecord(
                        change_set_id=change_set_id,
                        relative_path=str(change["relative_path"]),
                        operation=str(change["operation"]),
                        expected_sha256=change.get("expected_sha256"),
                        content_hash=str(change["content_hash"]),
                        before_size=int(change["before_size"]),
                        after_sha256=change.get("after_sha256"),
                        created_at=utc_now(),
                    )
                )

    def update_change_set_status(
        self, change_set_id: str, status: str, *, failure_reason: str | None = None
    ) -> None:
        with self._session_factory.begin() as session:
            record = session.get(ChangeSetRecord, change_set_id)
            if record is None:
                raise KeyError("unknown change set")
            record.status = status
            record.failure_reason = failure_reason

    def get_change_set(self, change_set_id: str) -> dict[str, Any] | None:
        with self._session_factory() as session:
            record = session.get(ChangeSetRecord, change_set_id)
            if record is None:
                return None
            changes = session.scalars(
                select(FileChangeRecord)
                .where(FileChangeRecord.change_set_id == change_set_id)
                .order_by(FileChangeRecord.id)
            ).all()
            return {
                "id": record.id,
                "task_id": record.task_id,
                "plan_id": record.plan_id,
                "project_id": record.project_id,
                "run_id": record.run_id,
                "project_path": record.project_path,
                "status": record.status,
                "patch_hash": record.patch_hash,
                "scope_hash": record.scope_hash,
                "before_manifest_json": record.before_manifest_json,
                "after_manifest_json": record.after_manifest_json,
                "rollback_manifest_json": record.rollback_manifest_json,
                "patches_json": record.patches_json,
                "snapshots_json": record.snapshots_json,
                "failure_reason": record.failure_reason,
                "file_changes": [
                    {
                        "relative_path": item.relative_path,
                        "operation": item.operation,
                        "expected_sha256": item.expected_sha256,
                        "content_hash": item.content_hash,
                        "before_size": item.before_size,
                        "after_sha256": item.after_sha256,
                    }
                    for item in changes
                ],
            }

    def list_change_set_ids(self) -> list[str]:
        with self._session_factory() as session:
            return [
                str(item)
                for (item,) in session.query(ChangeSetRecord.id)
                .filter(ChangeSetRecord.status.not_in({"reverted", "rejected", "expired"}))
                .all()
            ]

    def has_active_approval(self, change_set_id: str, capability: str) -> bool:
        with self._session_factory() as session:
            return (
                session.query(ApprovalRecord)
                .filter(
                    ApprovalRecord.change_set_id == change_set_id,
                    ApprovalRecord.capability == capability,
                    ApprovalRecord.status == "approved",
                    ApprovalRecord.consumed_at.is_(None),
                    ApprovalRecord.expires_at > utc_now(),
                )
                .first()
                is not None
            )

    def create_approval(
        self,
        *,
        approval_id: str,
        change_set_id: str,
        plan_hash: str,
        scope_hash: str,
        patch_hash: str,
        capability: str,
        token_digest: str,
        expires_at: datetime,
    ) -> None:
        with self._session_factory.begin() as session:
            session.add(
                ApprovalRecord(
                    id=approval_id,
                    change_set_id=change_set_id,
                    status="approved",
                    plan_hash=plan_hash,
                    scope_hash=scope_hash,
                    patch_hash=patch_hash,
                    capability=capability,
                    token_digest=token_digest,
                    expires_at=expires_at,
                    created_at=utc_now(),
                )
            )

    def consume_approval(self, token_digest: str, *, now: datetime) -> dict[str, Any] | None:
        """Atomically consume one unexpired approval and return its scope."""
        with self._session_factory.begin() as session:
            result = session.execute(
                update(ApprovalRecord)
                .where(
                    ApprovalRecord.token_digest == token_digest,
                    ApprovalRecord.status == "approved",
                    ApprovalRecord.consumed_at.is_(None),
                    ApprovalRecord.expires_at > now,
                )
                .values(status="consumed", consumed_at=now)
            )
            if int(cast(Any, result).rowcount or 0) != 1:
                return None
            approval = (
                session.query(ApprovalRecord)
                .filter(ApprovalRecord.token_digest == token_digest)
                .one()
            )
            return {
                "id": approval.id,
                "change_set_id": approval.change_set_id,
                "plan_hash": approval.plan_hash,
                "scope_hash": approval.scope_hash,
                "patch_hash": approval.patch_hash,
                "capability": approval.capability,
                "expires_at": approval.expires_at,
            }

    def peek_approval(self, token_digest: str, *, now: datetime) -> dict[str, Any] | None:
        with self._session_factory() as session:
            approval = (
                session.query(ApprovalRecord)
                .filter(
                    ApprovalRecord.token_digest == token_digest,
                    ApprovalRecord.status == "approved",
                    ApprovalRecord.consumed_at.is_(None),
                    ApprovalRecord.expires_at > now,
                )
                .one_or_none()
            )
            if approval is None:
                return None
            return {
                "id": approval.id,
                "change_set_id": approval.change_set_id,
                "plan_hash": approval.plan_hash,
                "scope_hash": approval.scope_hash,
                "patch_hash": approval.patch_hash,
                "capability": approval.capability,
                "expires_at": approval.expires_at,
            }

    def revoke_approvals(self, change_set_id: str) -> int:
        with self._session_factory.begin() as session:
            approvals = (
                session.query(ApprovalRecord)
                .filter(
                    ApprovalRecord.change_set_id == change_set_id,
                    ApprovalRecord.status == "approved",
                    ApprovalRecord.consumed_at.is_(None),
                )
                .all()
            )
            for approval in approvals:
                approval.status = "revoked"
            return len(approvals)

    def reserve_idempotency(self, key: str, request_hash: str) -> tuple[str, str | None]:
        """Reserve a key; return ``new``, ``replay`` or ``in_progress``."""
        with self._session_factory.begin() as session:
            result = session.execute(
                insert(IdempotencyRecord)
                .values(key=key, request_hash=request_hash, created_at=utc_now())
                .prefix_with("OR IGNORE")
            )
            record = session.get(IdempotencyRecord, key)
            if record is None:
                raise RuntimeError("idempotency reservation failed")
            if record.request_hash != request_hash:
                raise ValueError("idempotency key was reused with different content")
            if int(cast(Any, result).rowcount or 0) == 1:
                return "new", None
            if record.response_json is None:
                return "in_progress", None
            return "replay", record.response_json

    def complete_idempotency(self, key: str, response_json: str) -> None:
        with self._session_factory.begin() as session:
            record = session.get(IdempotencyRecord, key)
            if record is None:
                raise KeyError("unknown idempotency key")
            record.response_json = response_json

    def get_idempotency(self, key: str) -> tuple[str, str | None] | None:
        with self._session_factory() as session:
            record = session.get(IdempotencyRecord, key)
            return None if record is None else (record.request_hash, record.response_json)

    def list_applying_change_sets(self) -> list[tuple[str, str]]:
        with self._session_factory() as session:
            records = (
                session.query(ChangeSetRecord).filter(ChangeSetRecord.status == "applying").all()
            )
            return [
                (str(record.project_id), str(record.id))
                for record in records
                if record.project_id is not None
            ]

    def lock_project(self, project_id: str, reason: str) -> None:
        with self._session_factory.begin() as session:
            record = session.get(ProjectLockRecord, project_id)
            if record is None:
                session.add(
                    ProjectLockRecord(
                        project_id=project_id, reason=reason[:128], created_at=utc_now()
                    )
                )

    def is_project_locked(self, project_id: str) -> bool:
        with self._session_factory() as session:
            return session.get(ProjectLockRecord, project_id) is not None
