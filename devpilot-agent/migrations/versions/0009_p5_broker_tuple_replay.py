"""Prevent replay of an already consumed action tuple.

Revision ID: 0009_p5_broker_tuple_replay
Revises: 0008_p5_broker_actions
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_p5_broker_tuple_replay"
down_revision: str | None = "0008_p5_broker_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_broker_action_tuple",
        "broker_actions",
        [
            "session_id",
            "thread_id",
            "action",
            "change_set_id",
            "plan_hash",
            "patch_hash",
            "scope_hash",
            "project_id",
        ],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_broker_action_tuple", table_name="broker_actions")
