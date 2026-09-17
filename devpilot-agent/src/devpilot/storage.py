"""Small SQLite persistence layer for P1 business records."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    func,
    insert,
    select,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .redaction import redact_sensitive_payload

_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ProjectRecord(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    real_path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    trust_state: Mapped[str] = mapped_column(String(16), nullable=False, default="untrusted")
    repo_fingerprint: Mapped[str | None] = mapped_column(String(64))
    allowed_root_id: Mapped[str | None] = mapped_column(String(64))
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
    evidence_hash: Mapped[str | None] = mapped_column(String(64))
    contract_hash: Mapped[str | None] = mapped_column(String(64))
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
    contract_hash: Mapped[str | None] = mapped_column(String(64))
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


@dataclass(frozen=True)
class BugCheckFailureFact:
    """Server-computed failure fact accepted by the durable event store."""

    run_id: str
    project_id: str
    symptom: str
    expected_behavior: str
    target_files: tuple[str, ...]
    before_hashes: dict[str, str]
    code: str
    _server_token: object = field(init=False, repr=False, compare=False)

    @classmethod
    def _from_server(
        cls,
        *,
        run_id: str,
        project_id: str,
        symptom: str,
        expected_behavior: str,
        target_files: tuple[str, ...],
        before_hashes: dict[str, str],
        code: str,
    ) -> BugCheckFailureFact:
        fact = cls(
            run_id=run_id,
            project_id=project_id,
            symptom=symptom,
            expected_behavior=expected_behavior,
            target_files=target_files,
            before_hashes=before_hashes,
            code=code,
        )
        object.__setattr__(fact, "_server_token", _BUG_CHECK_SERVER_TOKEN)
        return fact


_BUG_CHECK_SERVER_TOKEN = object()


@dataclass(frozen=True)
class TestCheckFailureFact:
    """Server-computed failure fact for a saved Python or Node check."""

    run_id: str
    project_id: str
    symptom: str
    expected_behavior: str
    target_files: tuple[str, ...]
    before_hashes: dict[str, str]
    code: str
    _server_token: object = field(init=False, repr=False, compare=False)

    @classmethod
    def _from_server(
        cls,
        *,
        run_id: str,
        project_id: str,
        symptom: str,
        expected_behavior: str,
        target_files: tuple[str, ...],
        before_hashes: dict[str, str],
        code: str,
    ) -> TestCheckFailureFact:
        fact = cls(
            run_id=run_id,
            project_id=project_id,
            symptom=symptom,
            expected_behavior=expected_behavior,
            target_files=target_files,
            before_hashes=before_hashes,
            code=code,
        )
        object.__setattr__(fact, "_server_token", _TEST_CHECK_SERVER_TOKEN)
        return fact


_TEST_CHECK_SERVER_TOKEN = object()


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


class BrokerActionRecord(Base):
    """Durable, non-secret proof that one gateway confirmation was consumed."""

    __tablename__ = "broker_actions"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "thread_id",
            "confirmation_id",
            "user_event_nonce",
            name="uq_broker_action_confirmation",
        ),
        UniqueConstraint(
            "session_id", "thread_id", "user_message_seq", name="uq_broker_action_sequence"
        ),
        UniqueConstraint(
            "session_id",
            "thread_id",
            "action",
            "change_set_id",
            "plan_hash",
            "patch_hash",
            "scope_hash",
            "project_id",
            name="uq_broker_action_tuple",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    confirmation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_event_nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    user_message_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    change_set_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    patch_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    signature_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_digest: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="authorized")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_json: Mapped[str | None] = mapped_column(Text)


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
        # P8 callers require the P8 tables, not an obsolete final database
        # version.  The migration chain is linear through H9, so every listed
        # successor retains those tables.  Keep the compatibility set explicit
        # to fail closed for unknown/forked histories.
        compatible_successor_heads = {
            "0011_p8_skill_improvement_records": {
                "0012_h3_plugin_runtime_state",
                "0013_h8_loop_handoff_state",
                "0014_h8_requested_driver_state",
                "0015_h8_switch_generation",
                "0016_h9_harness_selection_records",
            }
        }
        compatible_head = actual in compatible_successor_heads.get(expected, set())
        if actual != expected and not compatible_head:
            raise RuntimeError(f"database schema head is {actual!r}; expected {expected!r}")

    def ensure_project(
        self,
        real_path: str,
        *,
        allowed_root_id: str | None = None,
        repo_fingerprint: str | None = None,
    ) -> str:
        with self._session_factory.begin() as session:
            existing = session.query(ProjectRecord).filter_by(real_path=real_path).one_or_none()
            if existing:
                if existing.trust_state != "trusted":
                    if allowed_root_id is not None:
                        existing.allowed_root_id = allowed_root_id
                    if repo_fingerprint is not None:
                        existing.repo_fingerprint = repo_fingerprint
                return existing.id
            project_id = str(uuid.uuid4())
            session.add(
                ProjectRecord(
                    id=project_id,
                    real_path=real_path,
                    trust_state="untrusted",
                    repo_fingerprint=repo_fingerprint,
                    allowed_root_id=allowed_root_id,
                    created_at=utc_now(),
                )
            )
            return project_id

    def get_project_path(self, project_id: str) -> str:
        with self._session_factory() as session:
            project = session.get(ProjectRecord, project_id)
            if project is None:
                raise KeyError("unknown project")
            return project.real_path

    def list_projects(self) -> list[dict[str, Any]]:
        """Return project summaries without exposing local filesystem paths."""
        with self._session_factory() as session:
            rows = (
                session.query(ProjectRecord)
                .order_by(ProjectRecord.created_at, ProjectRecord.id)
                .all()
            )
            return [
                {
                    "project_id": row.id,
                    "project_name": Path(row.real_path).name,
                    "trust_state": row.trust_state,
                    "repo_fingerprint": row.repo_fingerprint,
                    "allowed_root_id": row.allowed_root_id,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    def get_project_security(self, project_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            project = session.get(ProjectRecord, project_id)
            if project is None:
                raise KeyError("unknown project")
            return {
                "project_id": project.id,
                "project_name": Path(project.real_path).name,
                "trust_state": project.trust_state,
                "repo_fingerprint": project.repo_fingerprint,
                "allowed_root_id": project.allowed_root_id,
            }

    def trust_project(
        self,
        project_id: str,
        *,
        repo_fingerprint: str,
        allowed_root_id: str,
    ) -> dict[str, Any]:
        """Record an explicit operator trust decision for the current project binding."""
        with self._session_factory.begin() as session:
            project = session.get(ProjectRecord, project_id)
            if project is None:
                raise KeyError("unknown project")
            if project.repo_fingerprint not in {None, repo_fingerprint}:
                raise ValueError("repository fingerprint changed; re-register the project")
            if project.allowed_root_id not in {None, allowed_root_id}:
                raise ValueError("allowed root binding changed; re-register the project")
            project.repo_fingerprint = repo_fingerprint
            project.allowed_root_id = allowed_root_id
            project.trust_state = "trusted"
            return {
                "project_id": project.id,
                "trust_state": project.trust_state,
                "repo_fingerprint": project.repo_fingerprint,
                "allowed_root_id": project.allowed_root_id,
            }

    def require_trusted_project(
        self,
        project_id: str,
        *,
        repo_fingerprint: str,
        allowed_root_id: str,
    ) -> None:
        with self._session_factory() as session:
            project = session.get(ProjectRecord, project_id)
            if project is None:
                raise ValueError("project is not registered")
            if project.trust_state != "trusted":
                raise ValueError("project is not trusted")
            if project.repo_fingerprint != repo_fingerprint:
                raise ValueError("repository fingerprint does not match trusted project")
            if project.allowed_root_id != allowed_root_id:
                raise ValueError("allowed root binding does not match trusted project")

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
        if event_type == "bug_check.failed":
            raise ValueError("bug_check.failed must be produced by record_bug_check_failure")
        if event_type == "test_check.failed":
            raise ValueError("test_check.failed must be produced by record_test_check_failure")
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

    def record_bug_check_failure_fact(self, fact: BugCheckFailureFact) -> None:
        """Persist a fact whose file hashes were computed by a server-side checker."""
        self._record_check_failure_fact(
            fact,
            server_token=_BUG_CHECK_SERVER_TOKEN,
            run_kind="bug-check",
            event_type="bug_check.failed",
            producer="devpilot.bug-check",
            label="bug-check",
            creator="BugCheckService",
        )

    def record_test_check_failure_fact(self, fact: TestCheckFailureFact) -> None:
        """Persist a test-check fact whose hashes came from the server checker."""
        self._record_check_failure_fact(
            fact,
            server_token=_TEST_CHECK_SERVER_TOKEN,
            run_kind="test-check",
            event_type="test_check.failed",
            producer="devpilot.test-check",
            label="test-check",
            creator="TestCheckService",
        )

    def _record_check_failure_fact(
        self,
        fact: Any,
        *,
        server_token: object,
        run_kind: str,
        event_type: str,
        producer: str,
        label: str,
        creator: str,
    ) -> None:
        if getattr(fact, "_server_token", None) is not server_token:
            raise ValueError(f"{label} failure fact must be created by {creator}")
        if not fact.symptom.strip() or not fact.expected_behavior.strip() or not fact.code.strip():
            raise ValueError(f"{label} failure fields must be non-empty")
        if not fact.target_files or len(set(fact.target_files)) != len(fact.target_files):
            raise ValueError(f"{label} target files must be unique")
        canonical_targets = [self.canonical_failure_path(item) for item in fact.target_files]
        canonical_hashes: dict[str, str] = {}
        for path, value in fact.before_hashes.items():
            canonical_path = self.canonical_failure_path(path)
            if canonical_path in canonical_hashes and canonical_hashes[canonical_path] != value:
                raise ValueError(f"{label} before hashes conflict after canonicalization")
            canonical_hashes[canonical_path] = str(value)
        if len(set(canonical_targets)) != len(canonical_targets):
            raise ValueError(f"{label} target files must be unique after canonicalization")
        if any(
            not re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in canonical_hashes.values()
        ):
            raise ValueError(f"{label} before hashes must be SHA-256 values")
        if any(item not in canonical_hashes for item in canonical_targets):
            raise ValueError(f"{label} target files must have canonical before hashes")
        with self._session_factory.begin() as session:
            run = session.get(RunRecord, fact.run_id)
            if (
                run is None
                or run.project_id != fact.project_id
                or run.kind != run_kind
                or run.status != "running"
            ):
                raise ValueError(f"{label} failure must belong to an open {label} Run")
            project = session.get(ProjectRecord, fact.project_id)
            if project is None:
                raise ValueError(f"{label} project does not exist")
            project_path = Path(project.real_path).resolve(strict=True)
            for relative_path in canonical_targets:
                candidate = project_path / relative_path
                if candidate.is_symlink():
                    raise ValueError(f"{label} targets must be regular project files")
                target = candidate.resolve(strict=True)
                if not target.is_file() or not target.is_relative_to(project_path):
                    raise ValueError(f"{label} targets must be regular project files")
                actual_hash = hashlib.sha256(target.read_bytes()).hexdigest()
                if canonical_hashes[relative_path] != actual_hash:
                    raise ValueError(f"{label} before hash does not match the project file")
            payload = {
                "producer": producer,
                "schema_version": 1,
                "project_id": fact.project_id,
                "status": "failed",
                "code": fact.code.strip(),
                "symptom": fact.symptom.strip(),
                "expected_behavior": fact.expected_behavior.strip(),
                "target_files": canonical_targets,
                "before_hashes": canonical_hashes,
            }
            session.add(
                RunEventRecord(
                    run_id=fact.run_id,
                    event_type=event_type,
                    payload_json=json.dumps(
                        redact_sensitive_payload(payload),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    created_at=utc_now(),
                )
            )

    @staticmethod
    def canonical_failure_path(value: str) -> str:
        path = str(value).replace("\\", "/").strip()
        parts = tuple(item for item in path.split("/") if item)
        if (
            not path
            or path.startswith("/")
            or any(item in {".", ".."} or ":" in item for item in parts)
            or not parts
        ):
            raise ValueError("bug-check paths must be safe relative paths")
        normalized = "/".join(parts)
        return normalized.casefold() if os.name == "nt" else normalized

    def validate_open_run(self, *, project_id: str, run_id: str) -> None:
        with self._session_factory() as session:
            run = session.get(RunRecord, run_id)
            if run is None or run.project_id != project_id or run.status != "running":
                raise ValueError("run does not belong to project or is not open")

    def run_belongs_to_project(self, *, project_id: str, run_id: str) -> bool:
        with self._session_factory() as session:
            run = session.get(RunRecord, run_id)
            return run is not None and run.project_id == project_id

    def list_project_run_ids(self, *, project_id: str, kind: str) -> list[str]:
        """Return server-owned Runs for one project and kind, newest first."""
        with self._session_factory() as session:
            statement = (
                select(RunRecord.id)
                .where(RunRecord.project_id == project_id, RunRecord.kind == kind)
                .order_by(RunRecord.started_at.desc(), RunRecord.id.desc())
            )
            return list(session.scalars(statement))

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

    def get_run_state(self, run_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            run = session.get(RunRecord, run_id)
            if run is None:
                raise KeyError(f"unknown run: {run_id}")
            events = (
                session.query(RunEventRecord)
                .filter(RunEventRecord.run_id == run_id)
                .order_by(RunEventRecord.id)
                .all()
            )
            return {
                "run_id": run.id,
                "project_id": run.project_id,
                "kind": run.kind,
                "status": run.status,
                "started_at": run.started_at.isoformat(),
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "events": [
                    {
                        "event_id": event.id,
                        "run_id": event.run_id,
                        "sequence": sequence,
                        "correlation_id": event.run_id,
                        "event_type": event.event_type,
                        "payload": json.loads(event.payload_json),
                        "created_at": event.created_at.isoformat(),
                    }
                    for sequence, event in enumerate(events, start=1)
                ],
            }

    def list_run_events(self, run_id: str) -> list[dict[str, Any]]:
        """Return the append-only, already-redacted evidence for one Run."""
        with self._session_factory() as session:
            rows = (
                session.query(RunEventRecord)
                .filter(RunEventRecord.run_id == run_id)
                .order_by(RunEventRecord.id)
                .all()
            )
            return [
                {
                    "event_id": row.id,
                    "run_id": row.run_id,
                    "sequence": sequence,
                    "correlation_id": row.run_id,
                    "event_type": row.event_type,
                    "payload": json.loads(row.payload_json),
                    "created_at": row.created_at.isoformat(),
                }
                for sequence, row in enumerate(rows, start=1)
            ]

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
        kind: str = "markdown",
    ) -> None:
        with self._session_factory.begin() as session:
            session.add(
                ArtifactRecord(
                    id=artifact_id,
                    project_id=project_id,
                    run_id=run_id,
                    kind=kind,
                    relative_path=relative_path,
                    sha256=sha256,
                    size_bytes=size_bytes,
                    created_at=utc_now(),
                )
            )

    def count(self, model: type[Base]) -> int:
        with self._session_factory() as session:
            return int(session.query(model).count())

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = (
                session.query(ArtifactRecord)
                .filter(ArtifactRecord.run_id == run_id)
                .order_by(ArtifactRecord.created_at, ArtifactRecord.id)
                .all()
            )
            return [
                {
                    "id": row.id,
                    "project_id": row.project_id,
                    "run_id": row.run_id,
                    "kind": row.kind,
                    "relative_path": row.relative_path,
                    "sha256": row.sha256,
                    "size_bytes": row.size_bytes,
                }
                for row in rows
            ]

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            row = session.get(ArtifactRecord, artifact_id)
            if row is None:
                raise KeyError("unknown artifact")
            return {
                "id": row.id,
                "project_id": row.project_id,
                "run_id": row.run_id,
                "kind": row.kind,
                "relative_path": row.relative_path,
                "sha256": row.sha256,
                "size_bytes": row.size_bytes,
                "created_at": row.created_at.isoformat(),
            }

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
                "plan_contract": self.get_change_plan_contract(record.plan_id),
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

    def update_change_plan_contract(
        self,
        plan_id: str,
        *,
        check_ids: list[str],
        stop_conditions: list[str],
        evidence_hash: str,
        contract_hash: str,
    ) -> None:
        with self._session_factory.begin() as session:
            plan = session.get(ChangePlanRecord, plan_id)
            if plan is None:
                raise KeyError("unknown change plan")
            values = {
                "check_ids_json": json.dumps(check_ids, ensure_ascii=False, sort_keys=True),
                "stop_conditions_json": json.dumps(
                    stop_conditions, ensure_ascii=False, sort_keys=True
                ),
                "evidence_hash": evidence_hash,
                "contract_hash": contract_hash,
            }
            if plan.contract_hash is not None and any(
                getattr(plan, key) != value for key, value in values.items()
            ):
                raise ValueError("change plan contract is immutable")
            for key, value in values.items():
                setattr(plan, key, value)

    def get_change_plan_contract(self, plan_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            plan = session.get(ChangePlanRecord, plan_id)
            if plan is None:
                raise KeyError("unknown change plan")
            return {
                "plan_id": plan.id,
                "check_ids": json.loads(plan.check_ids_json),
                "stop_conditions": json.loads(plan.stop_conditions_json),
                "evidence_hash": plan.evidence_hash,
                "contract_hash": plan.contract_hash,
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
        contract_hash: str | None,
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
                    contract_hash=contract_hash,
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
                "contract_hash": approval.contract_hash,
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
                "contract_hash": approval.contract_hash,
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
            if key.startswith("candidate-operation-"):
                try:
                    payload = json.loads(record.response_json)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("candidate operation state is invalid") from exc
                if (
                    isinstance(payload, dict)
                    and payload.get("record_type") == "candidate_trial"
                    and payload.get("status") == "in_progress"
                ):
                    return "in_progress", record.response_json
            return "replay", record.response_json

    def complete_idempotency(self, key: str, response_json: str) -> None:
        with self._session_factory.begin() as session:
            record = session.get(IdempotencyRecord, key)
            if record is None:
                raise KeyError("unknown idempotency key")
            record.response_json = response_json

    def compare_and_set_idempotency(
        self, key: str, expected_response_json: str | None, response_json: str
    ) -> bool:
        """Replace one operation receipt only if its prior value is unchanged."""

        with self._session_factory.begin() as session:
            result = session.execute(
                update(IdempotencyRecord)
                .where(
                    IdempotencyRecord.key == key,
                    IdempotencyRecord.response_json == expected_response_json,
                )
                .values(response_json=response_json)
            )
            return int(cast(Any, result).rowcount or 0) == 1

    def bind_idempotency_request_id(self, key: str, request_id: str) -> str | None:
        """Atomically bind and return the identity used by a completed replay."""
        with self._session_factory.begin() as session:
            record = session.get(IdempotencyRecord, key)
            if record is None or record.response_json is None:
                return None
            original = record.response_json
            try:
                payload = json.loads(original)
            except (TypeError, ValueError):
                return None
            if not isinstance(payload, dict):
                return None
            typed_payload = cast(dict[str, Any], payload)
            existing = typed_payload.get("request_id")
            if isinstance(existing, str) and _REQUEST_ID_PATTERN.fullmatch(existing):
                return existing
            typed_payload["request_id"] = request_id
            updated = json.dumps(typed_payload, ensure_ascii=False, sort_keys=True)
            result = session.execute(
                update(IdempotencyRecord)
                .where(
                    IdempotencyRecord.key == key,
                    IdempotencyRecord.response_json == original,
                )
                .values(response_json=updated)
            )
            if int(cast(Any, result).rowcount or 0) == 1:
                return request_id
            refreshed = session.get(IdempotencyRecord, key)
            if refreshed is None or refreshed.response_json is None:
                return None
            try:
                refreshed_payload = json.loads(refreshed.response_json)
            except (TypeError, ValueError):
                return None
            if not isinstance(refreshed_payload, dict):
                return None
            refreshed_id = cast(dict[str, Any], refreshed_payload).get("request_id")
            return (
                refreshed_id
                if isinstance(refreshed_id, str) and _REQUEST_ID_PATTERN.fullmatch(refreshed_id)
                else None
            )

    def abort_idempotency(self, key: str) -> None:
        """Release a reservation whose operation failed before a response existed."""
        with self._session_factory.begin() as session:
            record = session.get(IdempotencyRecord, key)
            if record is not None and record.response_json is None:
                session.delete(record)

    def get_idempotency(self, key: str) -> tuple[str, str | None] | None:
        with self._session_factory() as session:
            record = session.get(IdempotencyRecord, key)
            return None if record is None else (record.request_hash, record.response_json)

    @staticmethod
    def _candidate_budget_key(task_id: str) -> str:
        return "candidate-budget-" + hashlib.sha256(task_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _candidate_budget_request_hash(task_id: str) -> str:
        return hashlib.sha256(f"candidate-budget:{task_id}".encode()).hexdigest()

    @staticmethod
    def _decode_candidate_budget(
        response_json: str | None,
    ) -> tuple[int, int, str | None, int, int]:
        if response_json is None:
            raise ValueError("candidate budget state is incomplete")
        try:
            payload = json.loads(response_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("candidate budget state is not valid JSON") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "trials",
            "reserved_seconds",
            "active_operation_id",
            "active_until_epoch",
            "lease_token",
        }:
            raise ValueError("candidate budget state has an invalid shape")
        trials = payload.get("trials")
        reserved_seconds = payload.get("reserved_seconds")
        active_operation_id = payload.get("active_operation_id")
        active_until_epoch = payload.get("active_until_epoch")
        lease_token = payload.get("lease_token")
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (trials, reserved_seconds)
        ):
            raise ValueError("candidate budget state has invalid counters")
        if active_operation_id is not None and not isinstance(active_operation_id, str):
            raise ValueError("candidate budget state has an invalid active operation")
        if (
            not isinstance(active_until_epoch, int)
            or isinstance(active_until_epoch, bool)
            or active_until_epoch < 0
        ):
            raise ValueError("candidate budget state has an invalid lease expiry")
        if active_operation_id is None and active_until_epoch != 0:
            raise ValueError("candidate budget state has an orphaned lease expiry")
        if active_operation_id is not None and active_until_epoch == 0:
            raise ValueError("candidate budget state has an active lease without expiry")
        if not isinstance(lease_token, int) or isinstance(lease_token, bool) or lease_token < 0:
            raise ValueError("candidate budget state has an invalid fencing token")
        return trials, reserved_seconds, active_operation_id, active_until_epoch, lease_token

    def reserve_candidate_budget(
        self,
        task_id: str,
        max_trials: int,
        max_trial_seconds: int,
        reservation_seconds: int,
        operation_id: str,
    ) -> tuple[bool, int, int, int]:
        """Atomically reserve one bounded candidate trial across processes."""

        if (
            not task_id
            or not operation_id
            or not 1 <= max_trials
            or not 1 <= max_trial_seconds
        ):
            raise ValueError("candidate budget arguments are invalid")
        if not 1 <= reservation_seconds <= max_trial_seconds:
            raise ValueError("candidate reservation is outside the task budget")
        key = self._candidate_budget_key(task_id)
        request_hash = self._candidate_budget_request_hash(task_id)
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    select(IdempotencyRecord.request_hash, IdempotencyRecord.response_json).where(
                        IdempotencyRecord.key == key
                    )
                ).first()
                if row is None:
                    trials = 0
                    reserved_seconds = 0
                    initial = {
                        "active_operation_id": None,
                        "active_until_epoch": 0,
                        "lease_token": 0,
                        "reserved_seconds": 0,
                        "trials": 0,
                    }
                    connection.execute(
                        insert(IdempotencyRecord).values(
                            key=key,
                            request_hash=request_hash,
                            response_json=json.dumps(initial, ensure_ascii=False, sort_keys=True),
                            created_at=utc_now(),
                        )
                    )
                    active_operation_id = None
                    active_until_epoch = 0
                    lease_token = 0
                else:
                    if row.request_hash != request_hash:
                        raise ValueError("candidate budget key was reused with different content")
                    (
                        trials,
                        reserved_seconds,
                        active_operation_id,
                        active_until_epoch,
                        lease_token,
                    ) = self._decode_candidate_budget(row.response_json)
                now_epoch = int(utc_now().timestamp())
                if (
                    active_operation_id is not None
                    and active_operation_id != operation_id
                    and active_until_epoch > now_epoch
                ):
                    connection.commit()
                    return False, trials, reserved_seconds, lease_token
                if active_operation_id == operation_id and active_until_epoch > now_epoch:
                    connection.commit()
                    return True, trials, reserved_seconds, lease_token
                if trials >= max_trials or (
                    reserved_seconds + reservation_seconds > max_trial_seconds
                ):
                    connection.commit()
                    return False, trials, reserved_seconds, lease_token
                updated = {
                    "active_operation_id": operation_id,
                    "active_until_epoch": now_epoch + max_trial_seconds,
                    "lease_token": lease_token + 1,
                    "reserved_seconds": reserved_seconds + reservation_seconds,
                    "trials": trials + 1,
                }
                connection.execute(
                    update(IdempotencyRecord)
                    .where(
                        IdempotencyRecord.key == key,
                        IdempotencyRecord.request_hash == request_hash,
                    )
                    .values(response_json=json.dumps(updated, ensure_ascii=False, sort_keys=True))
                )
                connection.commit()
                return (
                    True,
                    updated["trials"],
                    updated["reserved_seconds"],
                    updated["lease_token"],
                )
            except Exception:
                connection.rollback()
                raise

    def get_candidate_budget(self, task_id: str) -> tuple[int, int]:
        """Read the durable candidate budget; malformed state fails closed."""

        key = self._candidate_budget_key(task_id)
        request_hash = self._candidate_budget_request_hash(task_id)
        with self.engine.connect() as connection:
            row = connection.execute(
                select(IdempotencyRecord.request_hash, IdempotencyRecord.response_json).where(
                    IdempotencyRecord.key == key
                )
            ).first()
        if row is None:
            return 0, 0
        if row.request_hash != request_hash:
            raise ValueError("candidate budget key was reused with different content")
        trials, reserved_seconds, _active_operation_id, _active_until_epoch, _lease_token = (
            self._decode_candidate_budget(row.response_json)
        )
        return trials, reserved_seconds

    def validate_candidate_lease(
        self, task_id: str, operation_id: str, lease_token: int
    ) -> bool:
        key = self._candidate_budget_key(task_id)
        request_hash = self._candidate_budget_request_hash(task_id)
        with self.engine.connect() as connection:
            row = connection.execute(
                select(IdempotencyRecord.request_hash, IdempotencyRecord.response_json).where(
                    IdempotencyRecord.key == key
                )
            ).first()
        if row is None or row.request_hash != request_hash:
            return False
        _trials, _seconds, active_operation_id, active_until_epoch, current_token = (
            self._decode_candidate_budget(row.response_json)
        )
        return (
            active_operation_id == operation_id
            and current_token == lease_token
            and active_until_epoch > int(utc_now().timestamp())
        )

    def settle_candidate_budget(
        self, task_id: str, operation_id: str, lease_token: int | None
    ) -> bool:
        """Release a completed operation's active lease without refunding budget."""

        key = self._candidate_budget_key(task_id)
        request_hash = self._candidate_budget_request_hash(task_id)
        with self._session_factory.begin() as session:
            record = session.get(IdempotencyRecord, key)
            if record is None:
                return False
            if record.request_hash != request_hash:
                raise ValueError("candidate budget key was reused with different content")
            trials, reserved_seconds, active_operation_id, _active_until_epoch, current_token = (
                self._decode_candidate_budget(record.response_json)
            )
            if active_operation_id != operation_id or (
                lease_token is not None and current_token != lease_token
            ):
                return False
            payload = {
                "active_operation_id": None,
                "active_until_epoch": 0,
                "lease_token": current_token,
                "reserved_seconds": reserved_seconds,
                "trials": trials,
            }
            result = session.execute(
                update(IdempotencyRecord)
                .where(
                    IdempotencyRecord.key == key,
                    IdempotencyRecord.request_hash == request_hash,
                    IdempotencyRecord.response_json == record.response_json,
                )
                .values(response_json=json.dumps(payload, ensure_ascii=False, sort_keys=True))
            )
            return int(cast(Any, result).rowcount or 0) == 1

    def reserve_broker_action(self, payload: dict[str, Any]) -> bool:
        """Atomically consume a gateway confirmation before capability issuance."""
        with self._session_factory.begin() as session:
            existing_tuple = (
                session.query(BrokerActionRecord)
                .filter(
                    BrokerActionRecord.session_id == payload.get("session_id"),
                    BrokerActionRecord.thread_id == payload.get("thread_id"),
                    BrokerActionRecord.action == payload.get("action"),
                    BrokerActionRecord.change_set_id == payload.get("change_set_id"),
                    BrokerActionRecord.plan_hash == payload.get("plan_hash"),
                    BrokerActionRecord.patch_hash == payload.get("patch_hash"),
                    BrokerActionRecord.scope_hash == payload.get("scope_hash"),
                    BrokerActionRecord.project_id == payload.get("project_id"),
                )
                .one_or_none()
            )
            if existing_tuple is not None:
                # A retry after execution can recover the authorization only when
                # it is the same request. Fresh confirmation fields
                # must remain rejected by the durable tuple uniqueness boundary.
                if (
                    existing_tuple.status == "authorized"
                    and existing_tuple.confirmation_id == payload.get("confirmation_id")
                    and existing_tuple.user_event_nonce == payload.get("user_event_nonce")
                    and existing_tuple.user_message_seq == payload.get("user_message_seq")
                    and existing_tuple.signature_digest == payload.get("signature_digest")
                    and existing_tuple.idempotency_digest is not None
                    and existing_tuple.idempotency_digest == payload.get("idempotency_digest")
                ):
                    return True
                return False
            latest_sequence = (
                session.query(func.max(BrokerActionRecord.user_message_seq))
                .filter(
                    BrokerActionRecord.session_id == payload.get("session_id"),
                    BrokerActionRecord.thread_id == payload.get("thread_id"),
                )
                .scalar()
            )
            if latest_sequence is not None and int(payload["user_message_seq"]) <= int(
                latest_sequence
            ):
                return False
            record = BrokerActionRecord(id=str(uuid.uuid4()), created_at=utc_now(), **payload)
            try:
                session.add(record)
                session.flush()
            except Exception as exc:
                # SQLite uniqueness means replay or sequence re-use; hide DB details.
                if "UNIQUE constraint failed" in str(exc):
                    return False
                raise
            return True

    def finish_broker_action(
        self,
        confirmation_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        *,
        expected: Mapping[str, Any] | None = None,
    ) -> None:
        if status not in {"authorized", "completed", "rejected", "indeterminate"}:
            raise ValueError("invalid broker action status")
        with self._session_factory.begin() as session:
            query = session.query(BrokerActionRecord).filter(
                BrokerActionRecord.confirmation_id == confirmation_id,
                BrokerActionRecord.status == "authorized",
            )
            if expected is not None:
                for key in (
                    "session_id",
                    "thread_id",
                    "action",
                    "change_set_id",
                    "plan_hash",
                    "patch_hash",
                    "scope_hash",
                    "project_id",
                ):
                    query = query.filter(getattr(BrokerActionRecord, key) == expected.get(key))
            record = query.one_or_none()
            if record is not None:
                record.status = status
                record.finished_at = utc_now()
                if result is not None:
                    record.result_json = json.dumps(
                        redact_sensitive_payload(result), ensure_ascii=False, sort_keys=True
                    )

    def get_broker_action_result(self, payload: Mapping[str, Any]) -> dict[str, Any] | None:
        """Return a terminal result for an exact confirmation and action tuple."""
        keys = (
            "session_id",
            "thread_id",
            "confirmation_id",
            "user_event_nonce",
            "user_message_seq",
            "action",
            "change_set_id",
            "plan_hash",
            "patch_hash",
            "scope_hash",
            "project_id",
        )
        with self._session_factory() as session:
            query = session.query(BrokerActionRecord)
            for key in keys:
                query = query.filter(getattr(BrokerActionRecord, key) == payload.get(key))
            record = query.one_or_none()
            if record is None or record.result_json is None:
                return None
            try:
                result = json.loads(record.result_json)
            except (TypeError, ValueError):
                return None
            return cast(dict[str, Any], result) if isinstance(result, dict) else None

    def get_broker_action_idempotency_key(self, payload: Mapping[str, Any]) -> str | None:
        """Return the hidden key for an exact still-authorized retry."""
        keys = (
            "session_id",
            "thread_id",
            "confirmation_id",
            "user_event_nonce",
            "user_message_seq",
            "action",
            "change_set_id",
            "plan_hash",
            "patch_hash",
            "scope_hash",
            "project_id",
        )
        with self._session_factory() as session:
            query = session.query(BrokerActionRecord)
            for key in keys:
                query = query.filter(getattr(BrokerActionRecord, key) == payload.get(key))
            record = query.one_or_none()
            if record is None or record.status != "authorized":
                return None
            return record.idempotency_key

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
