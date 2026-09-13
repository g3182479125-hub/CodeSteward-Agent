"""Persist H8 loop-driver handoff ownership in the shared DevPilot database."""

from collections.abc import Sequence

from alembic import op

revision: str = "0013_h8_loop_handoff_state"
down_revision: str | None = "0012_h3_plugin_runtime_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists loop_handoff_state (
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
            updated_at varchar(64) not null
        )
        """
    )


def downgrade() -> None:
    op.drop_table("loop_handoff_state")
