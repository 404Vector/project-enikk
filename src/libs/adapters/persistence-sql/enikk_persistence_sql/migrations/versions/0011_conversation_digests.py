"""Daily conversation summaries (ADR-0035): their state, and indexes to read a day.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_digests",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("manual", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("day"),
    )
    op.create_index("ix_conversation_digests_status", "conversation_digests", ["status", "day"])
    op.create_index("ix_tasks_created", "tasks", ["created_at"])
    op.create_index("ix_outbox_created", "outbox", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_outbox_created", table_name="outbox")
    op.drop_index("ix_tasks_created", table_name="tasks")
    op.drop_index("ix_conversation_digests_status", table_name="conversation_digests")
    op.drop_table("conversation_digests")
