"""Persist the hidden Action Broker idempotency key for crash recovery.

Revision ID: 0010_p5_persist_idempotency_key
Revises: 0009_p5_broker_tuple_replay
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_p5_persist_idempotency_key"
down_revision: str | None = "0009_p5_broker_tuple_replay"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("broker_actions", sa.Column("idempotency_key", sa.String(length=128)))


def downgrade() -> None:
    op.drop_column("broker_actions", "idempotency_key")
