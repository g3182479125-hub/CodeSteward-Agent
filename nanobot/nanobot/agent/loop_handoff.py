"""Durable, boundary-only Loop Driver handoff for H8.

This module owns no LLM, tool, session, or permission operation.  It records
which immutable Driver artifact is allowed to decide at the *next committed
Host boundary*.  The record is stored in the existing DevPilot SQLite database
but in its own table, so it never overwrites plugin-manager state.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from nanobot.agent.loop_driver import (
    DefaultReactDriver,
    DriverAction,
    DriverActionKind,
    LoopCheckpoint,
    LoopDriver,
    PlanThenActDriver,
    RemoteStrategyDriver,
    StepInput,
)

_TABLE = "loop_handoff_state"


class DriverRegistry:
    """Small allow-list of trusted, immutable Driver artifacts."""

    def __init__(self) -> None:
        self._drivers: dict[tuple[str, str], type[LoopDriver]] = {
            ("default-react", "1"): DefaultReactDriver,
            ("plan-then-act", "1"): PlanThenActDriver,
            ("remote-strategy", "1"): RemoteStrategyDriver,
        }

    def restore(self, name: str, version: str, state: Mapping[str, object]) -> LoopDriver:
        driver_type = self._drivers.get((name, version))
        if driver_type is None:
            raise ValueError("unknown loop driver artifact")
        return driver_type.restore_state(state)


@dataclass(frozen=True, slots=True)
class HandoffRecord:
    session_id: str
    task_id: str
    project_id: str
    driver_name: str
    driver_version: str
    driver_state: Mapping[str, object]
    checkpoint: LoopCheckpoint
    owner_generation: int
    status: str = "active"
    operation_id: str | None = None
    requested_driver_name: str | None = None
    requested_driver_version: str | None = None
    requested_driver_state: Mapping[str, object] | None = None


class LoopHandoffStore(Protocol):
    def load(self, session_id: str) -> HandoffRecord | None: ...

    def save(self, record: HandoffRecord, *, expected_generation: int | None = None) -> HandoffRecord: ...


class SqliteLoopHandoffStore:
    """CAS persistence port backed by the Alembic-managed DevPilot database."""

    def __init__(self, database_path: str | Path, *, require_schema: bool = False) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.require_schema = require_schema
        if not require_schema:
            self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("pragma busy_timeout=30000")
        return connection

    def _ensure_table(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                self._ensure_table_on(connection)

    @staticmethod
    def _ensure_table_on(connection: sqlite3.Connection) -> None:
        connection.execute(
            f"""
            create table if not exists {_TABLE} (
                session_id text primary key,
                task_id text not null,
                project_id text not null,
                driver_name text not null,
                driver_version text not null,
                driver_state_json text not null,
                checkpoint_json text not null,
                owner_generation integer not null,
                status text not null,
                operation_id text,
                requested_driver_name text,
                requested_driver_version text,
                requested_driver_state_json text,
                updated_at text not null
            )
            """
        )

    def _check_table(self, connection: sqlite3.Connection) -> None:
        if not self.require_schema:
            return
        found = connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = ?", (_TABLE,)
        ).fetchone()
        if found is None:
            raise RuntimeError(f"{_TABLE} is missing; run the Alembic migration before enabling H8")

    @staticmethod
    def _row_to_record(row: tuple[object, ...]) -> HandoffRecord:
        checkpoint = LoopCheckpoint.from_dict(json.loads(str(row[6])))
        state = json.loads(str(row[5]))
        if not isinstance(state, dict):
            raise RuntimeError("loop handoff driver state must be an object")
        requested_state = json.loads(str(row[12])) if row[12] is not None else None
        if requested_state is not None and not isinstance(requested_state, dict):
            raise RuntimeError("requested loop driver state must be an object")
        return HandoffRecord(
            session_id=str(row[0]), task_id=str(row[1]), project_id=str(row[2]),
            driver_name=str(row[3]), driver_version=str(row[4]), driver_state=state,
            checkpoint=checkpoint, owner_generation=int(row[7]), status=str(row[8]),
            operation_id=str(row[9]) if row[9] is not None else None,
            requested_driver_name=str(row[10]) if row[10] is not None else None,
            requested_driver_version=str(row[11]) if row[11] is not None else None,
            requested_driver_state=requested_state,
        )

    def load(self, session_id: str) -> HandoffRecord | None:
        with closing(self._connect()) as connection:
            self._check_table(connection)
            row = connection.execute(
                f"select session_id, task_id, project_id, driver_name, driver_version, driver_state_json, checkpoint_json, owner_generation, status, operation_id, requested_driver_name, requested_driver_version, requested_driver_state_json from {_TABLE} where session_id = ?",
                (session_id,),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def save(self, record: HandoffRecord, *, expected_generation: int | None = None) -> HandoffRecord:
        payload = (
            record.session_id, record.task_id, record.project_id, record.driver_name, record.driver_version,
            json.dumps(dict(record.driver_state), ensure_ascii=False, sort_keys=True),
            json.dumps(record.checkpoint.to_dict(), ensure_ascii=False, sort_keys=True),
            record.owner_generation, record.status, record.operation_id,
            record.requested_driver_name, record.requested_driver_version,
            (
                json.dumps(dict(record.requested_driver_state), ensure_ascii=False, sort_keys=True)
                if record.requested_driver_state is not None
                else None
            ),
        )
        with closing(self._connect()) as connection:
            self._check_table(connection)
            with connection:
                connection.execute("begin immediate")
                if expected_generation is None:
                    existing = connection.execute(
                        f"select 1 from {_TABLE} where session_id = ?", (record.session_id,)
                    ).fetchone()
                    if existing is not None:
                        raise RuntimeError("loop handoff record already exists")
                    connection.execute(
                        f"insert into {_TABLE} (session_id, task_id, project_id, driver_name, driver_version, driver_state_json, checkpoint_json, owner_generation, status, operation_id, requested_driver_name, requested_driver_version, requested_driver_state_json, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                        payload,
                    )
                else:
                    result = connection.execute(
                        f"update {_TABLE} set task_id=?, project_id=?, driver_name=?, driver_version=?, driver_state_json=?, checkpoint_json=?, owner_generation=?, status=?, operation_id=?, requested_driver_name=?, requested_driver_version=?, requested_driver_state_json=?, updated_at=datetime('now') where session_id=? and owner_generation=?",
                        (*payload[1:], record.session_id, expected_generation),
                    )
                    if result.rowcount != 1:
                        raise RuntimeError("loop handoff ownership changed")
        return record


class LoopHandoffManager:
    """Coordinates requested switches without allowing stale owners to dispatch."""

    def __init__(self, store: LoopHandoffStore, registry: DriverRegistry | None = None) -> None:
        self.store = store
        self.registry = registry or DriverRegistry()

    def ensure(self, checkpoint: LoopCheckpoint, driver: LoopDriver | None = None) -> HandoffRecord:
        existing = self.store.load(checkpoint.session_id)
        if existing is not None:
            if existing.task_id != checkpoint.task_id or existing.project_id != checkpoint.project_id:
                raise RuntimeError("session is already owned by a different task")
            return existing
        active = driver or DefaultReactDriver()
        record = HandoffRecord(
            session_id=checkpoint.session_id, task_id=checkpoint.task_id, project_id=checkpoint.project_id,
            driver_name=active.name, driver_version=active.version, driver_state=active.export_state(),
            checkpoint=checkpoint, owner_generation=1,
        )
        return self.store.save(record)

    def request_switch(
        self,
        session_id: str,
        driver_name: str,
        driver_version: str,
        *,
        driver_state: Mapping[str, object] | None = None,
    ) -> HandoffRecord:
        current = self.store.load(session_id)
        if current is None:
            raise RuntimeError("loop handoff record does not exist")
        requested_state = driver_state or self._initial_state(driver_name, driver_version)
        self.registry.restore(driver_name, driver_version, requested_state)
        if current.status == "switch_pending":
            raise RuntimeError("a loop switch is already pending")
        requested = replace(
            current,
            owner_generation=current.owner_generation + 1,
            status="switch_pending",
            operation_id=str(uuid.uuid4()),
            requested_driver_name=driver_name,
            requested_driver_version=driver_version,
            requested_driver_state=dict(requested_state),
        )
        return self.store.save(requested, expected_generation=current.owner_generation)

    def operation_status(self, session_id: str, operation_id: str) -> HandoffRecord | None:
        record = self.store.load(session_id)
        if record is None or record.operation_id != operation_id:
            return None
        return record

    @staticmethod
    def _initial_state(name: str, version: str) -> Mapping[str, object]:
        if (name, version) == ("default-react", "1"):
            return DefaultReactDriver().export_state()
        if (name, version) == ("plan-then-act", "1"):
            return PlanThenActDriver().export_state()
        raise ValueError("unknown loop driver artifact")

    def commit_boundary(self, session_id: str, checkpoint: LoopCheckpoint) -> HandoffRecord:
        current = self.store.load(session_id)
        if current is None:
            raise RuntimeError("loop handoff record does not exist")
        if checkpoint.session_id != current.session_id or checkpoint.task_id != current.task_id:
            raise RuntimeError("checkpoint ownership does not match handoff")
        self._require_forward_checkpoint(current.checkpoint, checkpoint)
        if not checkpoint.switch_safe:
            # Persist latest facts but retain the original owner until safe.
            updated = replace(
                current,
                checkpoint=checkpoint, owner_generation=current.owner_generation + 1,
            )
            return self.store.save(updated, expected_generation=current.owner_generation)
        if current.status != "switch_pending":
            updated = replace(
                current,
                checkpoint=checkpoint, owner_generation=current.owner_generation + 1,
            )
            return self.store.save(updated, expected_generation=current.owner_generation)
        assert current.requested_driver_name is not None and current.requested_driver_version is not None
        state = current.requested_driver_state or self._initial_state(
            current.requested_driver_name, current.requested_driver_version
        )
        # Dry restore before committing; a failed init leaves the old artifact active.
        try:
            self.registry.restore(current.requested_driver_name, current.requested_driver_version, state)
        except ValueError:
            failed = replace(
                current,
                checkpoint=checkpoint,
                owner_generation=current.owner_generation + 1,
                status="switch_failed",
                requested_driver_name=None,
                requested_driver_version=None,
                requested_driver_state=None,
            )
            return self.store.save(failed, expected_generation=current.owner_generation)
        updated = HandoffRecord(
            session_id=current.session_id, task_id=current.task_id, project_id=current.project_id,
            driver_name=current.requested_driver_name, driver_version=current.requested_driver_version,
            driver_state=state, checkpoint=checkpoint, owner_generation=current.owner_generation + 1,
            status="active", operation_id=current.operation_id,
        )
        return self.store.save(updated, expected_generation=current.owner_generation)

    @staticmethod
    def _require_forward_checkpoint(previous: LoopCheckpoint, current: LoopCheckpoint) -> None:
        """Reject delayed writers before they can replace authoritative facts."""
        if (
            current.step < previous.step
            or current.message_position < previous.message_position
            or current.remaining_iterations > previous.remaining_iterations
            or not set(previous.completed_tool_results).issubset(current.completed_tool_results)
            or not set(previous.business_references.items()).issubset(current.business_references.items())
        ):
            raise RuntimeError("loop checkpoint regressed; stale producer rejected")

    def active_driver(self, session_id: str) -> tuple[LoopDriver, HandoffRecord]:
        record = self.store.load(session_id)
        if record is None:
            raise RuntimeError("loop handoff record does not exist")
        return self.registry.restore(record.driver_name, record.driver_version, record.driver_state), record

    def recover_driver(self, session_id: str) -> tuple[LoopDriver, HandoffRecord]:
        """Restore a driver or atomically return the task to default-react.

        This is intentionally recovery of the Driver only.  It never mutates
        the checkpoint, so an uncertain side effect remains paused/query-only.
        """
        record = self.store.load(session_id)
        if record is None:
            raise RuntimeError("loop handoff record does not exist")
        try:
            return self.registry.restore(record.driver_name, record.driver_version, record.driver_state), record
        except ValueError:
            fallback = DefaultReactDriver()
            recovered = replace(
                record,
                driver_name=fallback.name,
                driver_version=fallback.version,
                driver_state=fallback.export_state(),
                owner_generation=record.owner_generation + 1,
                status="recovered_default",
                requested_driver_name=None,
                requested_driver_version=None,
                requested_driver_state=None,
            )
            return fallback, self.store.save(recovered, expected_generation=record.owner_generation)


class ManagedLoopDriver:
    """Host-side indirection that rejects stale Driver ownership on every action.

    It deliberately resolves from the durable record instead of retaining the
    prior artifact.  A switch committed at a Host checkpoint is therefore seen
    by the next action without rebuilding the AgentLoop or its session.
    """

    def __init__(self, manager: LoopHandoffManager, session_id: str) -> None:
        self.manager = manager
        self.session_id = session_id
        self._last_record: HandoffRecord | None = None

    def _active(self) -> LoopDriver:
        driver, record = self.manager.recover_driver(self.session_id)
        self._last_record = record
        return driver

    @property
    def name(self) -> str:
        return self._active().name

    @property
    def version(self) -> str:
        return self._active().version

    def next_action(self, step: StepInput) -> DriverAction:
        driver = self._active()
        assert self._last_record is not None
        checkpoint = self._last_record.checkpoint
        if checkpoint.uncertain_side_effect_refs:
            return DriverAction(DriverActionKind.PAUSE, "uncertain_side_effect_requires_recovery")
        if checkpoint.remaining_iterations <= 0:
            return DriverAction(DriverActionKind.PAUSE, "durable_iteration_budget_exhausted")
        if step.step < checkpoint.step or step.message_position < checkpoint.message_position:
            return DriverAction(DriverActionKind.PAUSE, "stale_step_input_rejected")
        bounded = StepInput(
            session_id=step.session_id,
            task_id=step.task_id,
            project_id=step.project_id,
            step=step.step,
            message_position=step.message_position,
            remaining_iterations=min(step.remaining_iterations, checkpoint.remaining_iterations),
            completed_tool_results=step.completed_tool_results,
            pending_tool_calls=step.pending_tool_calls,
        )
        return driver.next_action(bounded)

    def export_state(self) -> Mapping[str, object]:
        return self._active().export_state()

    @classmethod
    def restore_state(cls, state: Mapping[str, object]) -> "ManagedLoopDriver":
        raise RuntimeError("managed loop driver is host-owned and cannot be restored directly")
