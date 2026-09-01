"""Add P2A-01 domain records and workflow state persistence."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_p2a01_domain"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("request_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("scope_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "change_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("risk_summary", sa.Text(), nullable=False),
        sa.Column("check_ids_json", sa.Text(), nullable=False),
        sa.Column("stop_conditions_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id", "version", name="uq_change_plan_task_version"),
    )
    op.create_table(
        "change_sets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("patch_hash", sa.String(length=64), nullable=False),
        sa.Column("before_manifest_json", sa.Text(), nullable=False),
        sa.Column("after_manifest_json", sa.Text(), nullable=False),
        sa.Column("rollback_manifest_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["change_plans.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("change_set_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("patch_hash", sa.String(length=64), nullable=False),
        sa.Column("capability", sa.String(length=32), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["change_set_id"], ["change_sets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "file_changes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("change_set_id", sa.String(length=36), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("expected_sha256", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("before_size", sa.Integer(), nullable=False),
        sa.Column("after_sha256", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["change_set_id"], ["change_sets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("change_set_id", "relative_path", name="uq_file_change_path"),
    )


def downgrade() -> None:
    op.drop_table("file_changes")
    op.drop_table("approvals")
    op.drop_table("change_sets")
    op.drop_table("change_plans")
    op.drop_table("tasks")
