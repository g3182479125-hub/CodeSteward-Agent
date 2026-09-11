"""Persist live Harness plugin runtime state in the existing DevPilot DB."""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_h3_plugin_runtime_state"
down_revision: str | None = "0011_p8_skill_improvement_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists plugin_runtime_state (
            id integer primary key check (id = 1),
            schema_version integer not null,
            state_json text not null,
            updated_at varchar(64) not null
        )
        """
    )


def downgrade() -> None:
    op.drop_table("plugin_runtime_state")
