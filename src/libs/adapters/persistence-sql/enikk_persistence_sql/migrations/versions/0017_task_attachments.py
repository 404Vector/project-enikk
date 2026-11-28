"""Files the owner attached to a task's message, until the task ends (ADR-0042 2b).

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_attachments",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column("detail", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id", "index"),
    )
    op.create_index(
        op.f("ix_task_attachments_created_at"), "task_attachments", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_task_attachments_created_at"), table_name="task_attachments")
    op.drop_table("task_attachments")
