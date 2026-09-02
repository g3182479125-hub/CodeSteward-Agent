"""Persist the Run associated with each ChangeSet for workflow recovery."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_p4_run_link"
down_revision = "0004_p2b_approval_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("change_sets", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("run_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key("fk_change_sets_run_id", "runs", ["run_id"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table("change_sets", recreate="always") as batch_op:
        batch_op.drop_constraint("fk_change_sets_run_id", type_="foreignkey")
        batch_op.drop_column("run_id")
