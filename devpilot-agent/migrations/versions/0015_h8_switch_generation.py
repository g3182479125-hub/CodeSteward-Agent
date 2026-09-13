"""Bind an H8 pending switch to its exact CAS generation."""

from collections.abc import Sequence

from alembic import op

revision: str = "0015_h8_switch_generation"
down_revision: str | None = "0014_h8_requested_driver_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("alter table loop_handoff_state add column switch_generation integer")


def downgrade() -> None:
    # SQLite migration remains append-only for supported deployments.
    pass
