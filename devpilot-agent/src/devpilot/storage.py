"""Small SQLite persistence layer for P1 business records."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


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
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    patch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    before_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    after_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    rollback_manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
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


class ProjectStore:
    """Repository-style operations; callers never build SQL strings."""

    def __init__(self, database_path: str | Path) -> None:
        path = Path(database_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{path}", future=True)
        self._session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

    def ensure_project(self, real_path: str) -> str:
        with self._session_factory.begin() as session:
            existing = session.query(ProjectRecord).filter_by(real_path=real_path).one_or_none()
            if existing:
                return existing.id
            project_id = str(uuid.uuid4())
            session.add(ProjectRecord(id=project_id, real_path=real_path, created_at=utc_now()))
            return project_id

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
        with self._session_factory.begin() as session:
            session.add(
                RunEventRecord(
                    run_id=run_id,
                    event_type=event_type,
                    payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    created_at=utc_now(),
                )
            )

    def validate_open_run(self, *, project_id: str, run_id: str) -> None:
        with self._session_factory() as session:
            run = session.get(RunRecord, run_id)
            if run is None or run.project_id != project_id or run.status != "running":
                raise ValueError("run does not belong to project or is not open")

    def finish_run(self, run_id: str, status: str, payload: dict[str, Any]) -> None:
        if status not in {"completed", "failed"}:
            raise ValueError("P1 runs can only finish as completed or failed")
        with self._session_factory.begin() as session:
            run = session.get(RunRecord, run_id)
            if run is None:
                raise ValueError(f"unknown run: {run_id}")
            run.status = status
            run.finished_at = utc_now()
            session.add(
                RunEventRecord(
                    run_id=run_id,
                    event_type=f"run.{status}",
                    payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
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
