"""Persist the complete P4 delivery contract and bind approvals to it."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_p4_contract_binding"
down_revision = "0005_p4_run_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("change_plans", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("evidence_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("contract_hash", sa.String(length=64), nullable=True))
    with op.batch_alter_table("approvals", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("contract_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("approvals", recreate="always") as batch_op:
        batch_op.drop_column("contract_hash")
    with op.batch_alter_table("change_plans", recreate="always") as batch_op:
        batch_op.drop_column("contract_hash")
        batch_op.drop_column("evidence_hash")
