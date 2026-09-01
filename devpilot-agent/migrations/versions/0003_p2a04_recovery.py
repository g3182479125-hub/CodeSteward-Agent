"""Persist P2A patch plans and recovery state."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_p2a04_recovery"
down_revision = "0002_p2a01_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("change_sets", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("project_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("project_path", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("patches_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("snapshots_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("failure_reason", sa.String(length=128), nullable=True))
        batch_op.create_foreign_key("fk_change_sets_project_id", "projects", ["project_id"], ["id"])
    op.create_table(
        "project_locks",
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("project_id"),
    )


def downgrade() -> None:
    op.drop_table("project_locks")
    with op.batch_alter_table("change_sets", recreate="always") as batch_op:
        batch_op.drop_constraint("fk_change_sets_project_id", type_="foreignkey")
        batch_op.drop_column("failure_reason")
        batch_op.drop_column("snapshots_json")
        batch_op.drop_column("patches_json")
        batch_op.drop_column("project_path")
        batch_op.drop_column("project_id")
