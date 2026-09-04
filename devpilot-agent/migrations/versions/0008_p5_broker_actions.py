"""Persist one-time gateway confirmation consumption for P5."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_p5_broker_actions"
down_revision = "0007_project_trust"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "broker_actions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("confirmation_id", sa.String(length=128), nullable=False),
        sa.Column("user_event_nonce", sa.String(length=128), nullable=False),
        sa.Column("user_message_seq", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("change_set_id", sa.String(length=128), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        sa.Column("patch_hash", sa.String(length=64), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("signature_digest", sa.String(length=64), nullable=False),
        sa.Column("idempotency_digest", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.UniqueConstraint("session_id", "thread_id", "confirmation_id", "user_event_nonce", name="uq_broker_action_confirmation"),
        sa.UniqueConstraint("session_id", "thread_id", "user_message_seq", name="uq_broker_action_sequence"),
    )


def downgrade() -> None:
    op.drop_table("broker_actions")
