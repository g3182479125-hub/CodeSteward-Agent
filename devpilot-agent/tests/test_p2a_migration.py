from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from devpilot.migration_config import resolve_database_url

ROOT = Path(__file__).resolve().parents[1]


def test_explicit_migration_database_url_wins_over_process_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEVPILOT_DATABASE_URL", "sqlite:///wrong.db")

    assert resolve_database_url("sqlite:///explicit.db") == "sqlite:///explicit.db"
    assert resolve_database_url("sqlite:///local-data/devpilot.db") == "sqlite:///wrong.db"


def migration_config(database_path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    return config


def test_p2a01_migration_creates_domain_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "empty.db"
    config = migration_config(database_path)

    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert {
            "tasks",
            "change_plans",
            "change_sets",
            "approvals",
            "file_changes",
            "project_locks",
        }.issubset(tables)
        with engine.connect() as connection:
            version = connection.execute(
                text("select version_num from alembic_version")
            ).scalar_one()
        assert version == "0011_p8_skill_improvement_records"
    finally:
        engine.dispose()


def test_p2a01_downgrade_removes_only_domain_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "downgrade.db"
    config = migration_config(database_path)

    command.upgrade(config, "head")
    command.downgrade(config, "0001_initial")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert "projects" in tables
        assert (
            not {
                "tasks",
                "change_plans",
                "change_sets",
                "approvals",
                "file_changes",
                "project_locks",
            }
            & tables
        )
        with engine.connect() as connection:
            version = connection.execute(
                text("select version_num from alembic_version")
            ).scalar_one()
        assert version == "0001_initial"
    finally:
        engine.dispose()


def test_p2a01_upgrade_preserves_p1_records(tmp_path: Path) -> None:
    database_path = tmp_path / "p1-data.db"
    config = migration_config(database_path)

    command.upgrade(config, "0001_initial")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "insert into projects (id, real_path, created_at) "
                    "values ('project-1', 'C:/fixture', '2026-08-31 00:00:00')"
                )
            )
            connection.execute(
                text(
                    "insert into runs (id, project_id, kind, status, started_at) "
                    "values ('run-1', 'project-1', 'repo-analysis', 'running', "
                    "'2026-08-31 00:00:00')"
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            project = connection.execute(
                text("select real_path from projects where id = 'project-1'")
            ).scalar_one()
            run_status = connection.execute(
                text("select status from runs where id = 'run-1'")
            ).scalar_one()
        assert project == "C:/fixture"
        assert run_status == "running"
    finally:
        engine.dispose()
