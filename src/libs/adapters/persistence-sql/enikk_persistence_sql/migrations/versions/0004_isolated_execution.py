"""Tasks record where they run; Jobs started for isolated tasks are kept (ADR-0021).

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("execution", sa.String(length=16), nullable=False, server_default="shared"),
    )
    op.alter_column("tasks", "execution", server_default=None)
    op.create_table(
        "dispatches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("job_name", sa.String(length=63), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dispatches_created", "dispatches", ["created_at"])
    op.create_index("ix_dispatches_task", "dispatches", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_dispatches_task", table_name="dispatches")
    op.drop_index("ix_dispatches_created", table_name="dispatches")
    op.drop_table("dispatches")
    op.drop_column("tasks", "execution")
