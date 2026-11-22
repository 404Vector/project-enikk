"""Outbox messages carry the inbound event their task answers (ADR-0018).

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox", sa.Column("source_event_id", sa.String(length=255), nullable=True))
    op.execute(
        "UPDATE outbox SET source_event_id = tasks.source_event_id "
        "FROM tasks WHERE tasks.id = outbox.task_id"
    )
    op.alter_column("outbox", "source_event_id", nullable=False)


def downgrade() -> None:
    op.drop_column("outbox", "source_event_id")
