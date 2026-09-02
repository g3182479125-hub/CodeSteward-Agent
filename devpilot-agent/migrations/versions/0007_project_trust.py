"""Persist explicit project trust and source-boundary identity."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_project_trust"
down_revision = "0006_p4_contract_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("projects", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column("trust_state", sa.String(length=16), nullable=False, server_default="untrusted")
        )
        batch_op.add_column(sa.Column("repo_fingerprint", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("allowed_root_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("projects", recreate="always") as batch_op:
        batch_op.drop_column("allowed_root_id")
        batch_op.drop_column("repo_fingerprint")
        batch_op.drop_column("trust_state")
