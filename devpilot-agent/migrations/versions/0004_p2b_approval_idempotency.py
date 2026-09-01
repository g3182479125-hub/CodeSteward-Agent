"""Persist P2B approval scope and idempotency reservations."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_p2b_approval_idempotency"
down_revision = "0003_p2a04_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("change_sets", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("scope_hash", sa.String(length=64), nullable=True))
    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("idempotency_keys")
    with op.batch_alter_table("change_sets", recreate="always") as batch_op:
        batch_op.drop_column("scope_hash")
