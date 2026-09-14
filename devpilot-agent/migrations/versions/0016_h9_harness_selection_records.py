"""Store H9 Harness candidate-selection records in the DevPilot database."""

from collections.abc import Sequence

from alembic import op

revision: str = "0016_h9_harness_selection_records"
down_revision: str | None = "0015_h8_switch_generation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        create table harness_selection_records (
            record_id varchar(128) primary key,
            record_type varchar(32) not null,
            payload_json text not null,
            record_mac varchar(64) not null,
            created_at varchar(64) not null
        )
        """
    )


def downgrade() -> None:
    op.drop_table("harness_selection_records")
