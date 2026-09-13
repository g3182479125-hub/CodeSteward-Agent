"""Persist an immutable requested H8 Driver artifact before ownership transfer."""

from collections.abc import Sequence

from alembic import op

revision: str = "0014_h8_requested_driver_state"
down_revision: str | None = "0013_h8_loop_handoff_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("alter table loop_handoff_state add column requested_driver_state_json text")


def downgrade() -> None:
    # SQLite cannot safely drop a column in the project's supported deployment
    # range. The forward migration is intentionally append-only.
    pass
