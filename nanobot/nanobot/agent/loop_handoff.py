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
    switch_generation: int | None = None


class LoopHandoffStore(Protocol):
    def load(self, session_id: str) -> HandoffRecord | None: ...

    def delete(self, session_id: str) -> None: ...

    def save(
        self, record: HandoffRecord, *, expected_generation: int | None = None
    ) -> HandoffRecord: ...


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
                switch_generation integer,
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
            session_id=str(row[0]),
            task_id=str(row[1]),
            project_id=str(row[2]),
            driver_name=str(row[3]),
            driver_version=str(row[4]),
            driver_state=state,
            checkpoint=checkpoint,
            owner_generation=int(row[7]),
            status=str(row[8]),
            operation_id=str(row[9]) if row[9] is not None else None,
            requested_driver_name=str(row[10]) if row[10] is not None else None,
            requested_driver_version=str(row[11]) if row[11] is not None else None,
            requested_driver_state=requested_state,
            switch_generation=int(row[13]) if row[13] is not None else None,
        )

    def load(self, session_id: str) -> HandoffRecord | None:
        with closing(self._connect()) as connection:
            self._check_table(connection)
            row = connection.execute(
                f"select session_id, task_id, project_id, driver_name, driver_version, driver_state_json, checkpoint_json, owner_generation, status, operation_id, requested_driver_name, requested_driver_version, requested_driver_state_json, switch_generation from {_TABLE} where session_id = ?",
                (session_id,),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def save(
        self, record: HandoffRecord, *, expected_generation: int | None = None
    ) -> HandoffRecord:
        payload = (
            record.session_id,
            record.task_id,
            record.project_id,
            record.driver_name,
            record.driver_version,
            json.dumps(dict(record.driver_state), ensure_ascii=False, sort_keys=True),
            json.dumps(record.checkpoint.to_dict(), ensure_ascii=False, sort_keys=True),
            record.owner_generation,
            record.status,
            record.operation_id,
            record.requested_driver_name,
            record.requested_driver_version,
            (
                json.dumps(dict(record.requested_driver_state), ensure_ascii=False, sort_keys=True)
                if record.requested_driver_state is not None
                else None
            ),
            record.switch_generation,
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
                        f"insert into {_TABLE} (session_id, task_id, project_id, driver_name, driver_version, driver_state_json, checkpoint_json, owner_generation, status, operation_id, requested_driver_name, requested_driver_version, requested_driver_state_json, switch_generation, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                        payload,
                    )
                else:
                    result = connection.execute(
                        f"update {_TABLE} set task_id=?, project_id=?, driver_name=?, driver_version=?, driver_state_json=?, checkpoint_json=?, owner_generation=?, status=?, operation_id=?, requested_driver_name=?, requested_driver_version=?, requested_driver_state_json=?, switch_generation=?, updated_at=datetime('now') where session_id=? and owner_generation=?",
                        (*payload[1:], record.session_id, expected_generation),
                    )
                    if result.rowcount != 1:
                        raise RuntimeError("loop handoff ownership changed")
        return record

    def delete(self, session_id: str) -> None:
        with closing(self._connect()) as connection:
            self._check_table(connection)
            with connection:
                connection.execute(
                    f"delete from {_TABLE} where session_id = ?", (session_id,)
                )


class LoopHandoffManager:
    """Coordinates requested switches without allowing stale owners to dispatch."""

    def __init__(self, store: LoopHandoffStore, registry: DriverRegistry | None = None) -> None:
        self.store = store
        self.registry = registry or DriverRegistry()

    def ensure(self, checkpoint: LoopCheckpoint, driver: LoopDriver | None = None) -> HandoffRecord:
        existing = self.store.load(checkpoint.session_id)
        if existing is not None:
            if (
                existing.task_id != checkpoint.task_id
                or existing.project_id != checkpoint.project_id
            ):
                raise RuntimeError("session is already owned by a different task")
            return existing
        active = driver or DefaultReactDriver()
        record = HandoffRecord(
            session_id=checkpoint.session_id,
            task_id=checkpoint.task_id,
            project_id=checkpoint.project_id,
            driver_name=active.name,
            driver_version=active.version,
            driver_state=active.export_state(),
            checkpoint=checkpoint,
            owner_generation=1,
        )
        return self.store.save(record)

    def reset(self, session_id: str) -> None:
        """Forget the active H8 owner when the user explicitly starts /new."""
        self.store.delete(session_id)

    def initial_driver(self, name: str, version: str) -> LoopDriver:
        """Resolve a trusted H9 selection only when a task has no owner yet."""
        return self.registry.restore(name, version, self._initial_state(name, version))

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
            # Persist the queue transition so stale raw writes are rejected.
            # The managed driver recognizes exactly this one-generation
            # pending window as belonging to the still-active old driver.
            owner_generation=current.owner_generation + 1,
            status="switch_pending",
            operation_id=str(uuid.uuid4()),
            requested_driver_name=driver_name,
            requested_driver_version=driver_version,
            requested_driver_state=dict(requested_state),
            switch_generation=current.owner_generation + 1,
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

    def commit_boundary(
        self,
        session_id: str,
        checkpoint: LoopCheckpoint,
        *,
        expected_generation: int | None = None,
    ) -> HandoffRecord:
        current = self.store.load(session_id)
        if current is None:
            raise RuntimeError("loop handoff record does not exist")
        if checkpoint.session_id != current.session_id or checkpoint.task_id != current.task_id:
            raise RuntimeError("checkpoint ownership does not match handoff")
        if expected_generation is not None and current.owner_generation != expected_generation:
            raise RuntimeError("loop handoff ownership changed")
        self._require_forward_checkpoint(current.checkpoint, checkpoint)
        if not checkpoint.switch_safe:
            # Persist latest facts but retain the original owner until safe.
            updated = replace(
                current,
                checkpoint=checkpoint,
                # Facts such as an uncertain external run are an ownership
                # boundary too. Advancing the CAS generation prevents another
                # producer that observed the previous safe checkpoint from
                # clearing or replacing those facts.
                owner_generation=current.owner_generation + 1,
            )
            return self.store.save(updated, expected_generation=current.owner_generation)
        if current.status != "switch_pending":
            updated = replace(
                current,
                checkpoint=checkpoint,
                owner_generation=current.owner_generation + 1,
            )
            return self.store.save(updated, expected_generation=current.owner_generation)
        assert (
            current.requested_driver_name is not None
            and current.requested_driver_version is not None
        )
        state = current.requested_driver_state or self._initial_state(
            current.requested_driver_name, current.requested_driver_version
        )
        # Dry restore before committing; a failed init leaves the old artifact active.
        try:
            self.registry.restore(
                current.requested_driver_name, current.requested_driver_version, state
            )
        except ValueError:
            failed = replace(
                current,
                checkpoint=checkpoint,
                owner_generation=current.owner_generation + 1,
                status="switch_failed",
                requested_driver_name=None,
                requested_driver_version=None,
                requested_driver_state=None,
                switch_generation=None,
            )
            return self.store.save(failed, expected_generation=current.owner_generation)
        updated = HandoffRecord(
            session_id=current.session_id,
            task_id=current.task_id,
            project_id=current.project_id,
            driver_name=current.requested_driver_name,
            driver_version=current.requested_driver_version,
            driver_state=state,
            checkpoint=checkpoint,
            owner_generation=current.owner_generation + 1,
            status="active",
            operation_id=current.operation_id,
            switch_generation=None,
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
            or not set(previous.business_references.items()).issubset(
                current.business_references.items()
            )
        ):
            raise RuntimeError("loop checkpoint regressed; stale producer rejected")

    def active_driver(self, session_id: str) -> tuple[LoopDriver, HandoffRecord]:
        record = self.store.load(session_id)
        if record is None:
            raise RuntimeError("loop handoff record does not exist")
        return self.registry.restore(
            record.driver_name, record.driver_version, record.driver_state
        ), record

    def recover_driver(self, session_id: str) -> tuple[LoopDriver, HandoffRecord]:
        """Restore a driver or atomically return the task to default-react.

        This is intentionally recovery of the Driver only.  It never mutates
        the checkpoint, so an uncertain side effect remains paused/query-only.
        """
        record = self.store.load(session_id)
        if record is None:
            raise RuntimeError("loop handoff record does not exist")
        try:
            return self.registry.restore(
                record.driver_name, record.driver_version, record.driver_state
            ), record
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
                switch_generation=None,
            )
            return fallback, self.store.save(recovered, expected_generation=record.owner_generation)


class ManagedLoopDriver:
    """Host-side indirection that rejects stale Driver ownership on every action.

    It deliberately resolves from the durable record instead of retaining the
    prior artifact.  A switch committed at a Host checkpoint is therefore seen
    by the next action without rebuilding the AgentLoop or its session.
    """

    def __init__(
        self,
        manager: LoopHandoffManager,
        session_id: str,
        *,
        allow_unresolved_side_effects: bool = False,
    ) -> None:
        self.manager = manager
        self.session_id = session_id
        self.allow_unresolved_side_effects = allow_unresolved_side_effects
        self._last_record: HandoffRecord | None = None
        self._last_decision_step: StepInput | None = None
        self._dispatch_generation: int | None = None
        self._dispatch_driver_identity: tuple[str, str] | None = None

    def _active(self) -> LoopDriver:
        driver, record = self.manager.recover_driver(self.session_id)
        # This is intentionally *not* a token refresh. A stale runner may
        # read name/version while a different owner has already switched the
        # durable record; only its own successful checkpoint may advance the
        # producer token through ``note_checkpoint_committed``.
        if self._last_record is None:
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
        current = self.manager.store.load(self.session_id)
        if current is None:
            return DriverAction(DriverActionKind.PAUSE, "handoff_record_missing")
        held = self._last_record
        owns_current_route = current.owner_generation == held.owner_generation or (
            current.status == "switch_pending"
            and current.owner_generation == held.owner_generation + 1
            and (current.driver_name, current.driver_version)
            == (held.driver_name, held.driver_version)
        )
        if not owns_current_route:
            # No model or remote-strategy call may be made by a stale actor.
            # It has to observe the committed route on a new Host boundary.
            return DriverAction(DriverActionKind.PAUSE, "stale_driver_owner_rejected")
        checkpoint = self._last_record.checkpoint
        # ``tool_call:`` entries mean the current Host process has persisted
        # an intent and is about to settle that exact in-process tool call.
        # They are not an externally unknown side effect, so pausing here
        # would make every H8 tool turn impossible. All real business refs
        # (run/operation/sandbox/approval) still require query-only recovery.
        unresolved_external_refs = tuple(
            ref for ref in checkpoint.uncertain_side_effect_refs if not ref.startswith("tool_call:")
        )
        if unresolved_external_refs and not self.allow_unresolved_side_effects:
            return DriverAction(DriverActionKind.PAUSE, "uncertain_side_effect_requires_recovery")
        if step.step < checkpoint.step or step.message_position < checkpoint.message_position:
            return DriverAction(DriverActionKind.PAUSE, "stale_step_input_rejected")
        bounded = StepInput(
            session_id=step.session_id,
            task_id=step.task_id,
            project_id=step.project_id,
            step=step.step,
            message_position=step.message_position,
            remaining_iterations=(
                step.remaining_iterations
                if self.allow_unresolved_side_effects
                else min(step.remaining_iterations, checkpoint.remaining_iterations)
            ),
            completed_tool_results=step.completed_tool_results,
            pending_tool_calls=step.pending_tool_calls,
        )
        self._last_decision_step = bounded
        # A model response produced within the final permitted iteration is
        # still allowed to settle the tools it already requested. The budget
        # only prevents another model decision after that settlement.
        if (
            checkpoint.remaining_iterations <= 0
            and not bounded.pending_tool_calls
            and not self.allow_unresolved_side_effects
        ):
            return DriverAction(DriverActionKind.PAUSE, "durable_iteration_budget_exhausted")
        action = driver.next_action(bounded)
        self._dispatch_generation = (
            self._last_record.owner_generation
            if action.kind is DriverActionKind.DISPATCH_TOOLS
            else None
        )
        self._dispatch_driver_identity = (
            (self._last_record.driver_name, self._last_record.driver_version)
            if action.kind is DriverActionKind.DISPATCH_TOOLS
            else None
        )
        return action

    def decision_step(self) -> StepInput | None:
        """Return the Host-bounded facts used for the latest Driver decision."""
        return self._last_decision_step

    def checkpoint_generation(self) -> int | None:
        """Return the generation held by this runner's durable producer."""
        if self._last_record is not None:
            try:
                current = self.manager.store.load(self.session_id)
            except (OSError, RuntimeError, ValueError):
                current = None
            # A single queued switch is still owned by the old active driver
            # until it settles pending work. Do not accept a later ABA return.
            if (
                current is not None
                and current.status == "switch_pending"
                and current.owner_generation == self._last_record.owner_generation + 1
                and current.switch_generation == current.owner_generation
                and (current.driver_name, current.driver_version)
                == (self._last_record.driver_name, self._last_record.driver_version)
            ):
                return current.owner_generation
            return self._last_record.owner_generation
        try:
            record = self.manager.store.load(self.session_id)
        except (OSError, RuntimeError, ValueError):
            return None
        return record.owner_generation if record is not None else None

    def note_checkpoint_committed(self, record: HandoffRecord) -> None:
        """Advance this producer's token only after its CAS write succeeds."""
        self._last_record = record

    def dispatch_is_current(self) -> bool:
        """Confirm that the owner which chose dispatch still owns the record."""
        generation = self._dispatch_generation
        identity = self._dispatch_driver_identity
        if generation is None or identity is None:
            return False
        try:
            record = self.manager.store.load(self.session_id)
        except (OSError, RuntimeError, ValueError):
            return False
        return (
            record is not None
            # A queued switch increments the generation but deliberately
            # retains the old driver until its pending tools have settled.
            # The driver identity, rather than that queue-only generation,
            # is the authority for this already-approved in-flight dispatch.
            and (
                record.owner_generation == generation
                or (
                    record.status == "switch_pending"
                    and record.owner_generation == generation + 1
                    and record.switch_generation == record.owner_generation
                )
            )
            and (record.driver_name, record.driver_version) == identity
            and record.status not in {"switch_failed", "failed"}
        )

    def export_state(self) -> Mapping[str, object]:
        return self._active().export_state()

    @classmethod
    def restore_state(cls, state: Mapping[str, object]) -> "ManagedLoopDriver":
        raise RuntimeError("managed loop driver is host-owned and cannot be restored directly")
