"""Store P8 Skill improvement records in the existing DevPilot database."""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_p8_skill_improvement_records"
down_revision: str | None = "0010_p5_persist_idempotency_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        create table skill_improvement_records (
            id integer primary key autoincrement,
            record_key varchar(256) not null unique,
            record_type varchar(64) not null,
            record_id varchar(256) not null,
            payload_json text not null,
            record_mac varchar(64) not null,
            workspace text,
            project_id text,
            created_at varchar(64) not null
        )
        """
    )


def downgrade() -> None:
    op.drop_table("skill_improvement_records")
