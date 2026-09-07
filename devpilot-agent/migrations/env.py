"""Alembic environment for the DevPilot SQLite store."""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from devpilot.migration_config import resolve_database_url  # noqa: E402
from devpilot.storage import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option(
    "sqlalchemy.url",
    resolve_database_url(config.get_main_option("sqlalchemy.url")),
)


def _ensure_sqlite_parent() -> None:
    """Create the parent for a file-backed SQLite migration target."""
    database_url = make_url(config.get_main_option("sqlalchemy.url"))
    if database_url.get_backend_name() != "sqlite" or not database_url.database:
        return
    if database_url.database == ":memory:":
        return
    Path(database_url.database).expanduser().resolve().parent.mkdir(
        parents=True, exist_ok=True
    )


_ensure_sqlite_parent()
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
