"""Durable state boundary for live plugin routing.

The DevPilot host already owns the SQLite database and its Alembic schema.
This module deliberately exposes only a small JSON document port so the
nanobot runtime does not import the DevPilot ORM or create a second store.
Standalone tests may use the same port against a temporary SQLite file.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast

_TABLE = "plugin_runtime_state"


class PluginStateStore(Protocol):
    """Host-owned persistence port for plugin versions and operations."""

    def load(self) -> dict[str, Any] | None:
        """Load the latest state document, if one exists."""

    def save(self, state: Mapping[str, Any]) -> None:
        """Atomically replace the latest state document."""


class SqlitePluginStateStore:
    """Use the existing Alembic-managed SQLite database as the state store."""

    def __init__(self, database_path: str | Path, *, require_schema: bool = False) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._require_schema = require_schema
        if not require_schema:
            self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("pragma busy_timeout=30000")
        return connection

    def _ensure_table(self) -> None:
        with self._connect() as connection:
            connection.execute(
                f"""
                create table if not exists {_TABLE} (
                    id integer primary key check (id = 1),
                    schema_version integer not null,
                    state_json text not null,
                    updated_at text not null
                )
                """
            )

    def _check_table(self, connection: sqlite3.Connection) -> None:
        if not self._require_schema:
            return
        found = connection.execute(
            "select 1 from sqlite_master where type = 'table' and name = ?",
            (_TABLE,),
        ).fetchone()
        if found is None:
            # A running nanobot may start against a DevPilot database that was
            # stopped one migration before H3.  Add the H3 table only when the
            # database identifies itself as the known 0011 schema; the next
            # Alembic upgrade is idempotent and records the real head.
            try:
                version_row = connection.execute(
                    "select version_num from alembic_version"
                ).fetchone()
            except sqlite3.OperationalError as exc:
                raise RuntimeError(
                    f"{_TABLE} is missing; run the Alembic migration before enabling live plugins"
                ) from exc
            if version_row is None or str(version_row[0]) != "0011_p8_skill_improvement_records":
                raise RuntimeError(
                    f"{_TABLE} is missing; run the Alembic migration before enabling live plugins"
                )
            self._ensure_table_on_connection(connection)

    @staticmethod
    def _ensure_table_on_connection(connection: sqlite3.Connection) -> None:
        connection.execute(
            f"""
            create table if not exists {_TABLE} (
                id integer primary key check (id = 1),
                schema_version integer not null,
                state_json text not null,
                updated_at text not null
            )
            """
        )

    def load(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            self._check_table(connection)
            row = connection.execute(
                f"select state_json from {_TABLE} where id = 1"
            ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row[0]))
        if not isinstance(value, dict):
            raise RuntimeError("plugin runtime state must be a JSON object")
        return cast(dict[str, Any], value)

    def save(self, state: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(state), ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            self._check_table(connection)
            connection.execute(
                f"""
                insert into {_TABLE} (id, schema_version, state_json, updated_at)
                values (1, ?, ?, datetime('now'))
                on conflict(id) do update set
                    schema_version = excluded.schema_version,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (1, payload),
            )
