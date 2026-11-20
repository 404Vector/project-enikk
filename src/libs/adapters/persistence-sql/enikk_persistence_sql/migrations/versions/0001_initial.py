"""Initial schema: sessions, tasks, outbox.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connector", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=255), nullable=False),
        sa.Column("thread", sa.String(length=255), nullable=False),
        sa.Column("agent", sa.String(length=63), nullable=False),
        sa.Column("engine", sa.String(length=16), nullable=False),
        sa.Column("engine_session_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connector", "channel", "thread", name="uq_sessions_conversation"),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("source_event_id", sa.String(length=255), nullable=False),
        sa.Column("agent", sa.String(length=63), nullable=False),
        sa.Column("engine", sa.String(length=16), nullable=False),
        sa.Column("input", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_lease_id", sa.Uuid(), nullable=True),
        sa.Column("progress", sa.String(length=500), nullable=True),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("failure", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_event_id"),
    )
    op.create_index("ix_tasks_queue", "tasks", ["engine", "status", "created_at"])
    op.create_index("ix_tasks_session_status", "tasks", ["session_id", "status"])
    op.create_index("ix_tasks_lease_expiry", "tasks", ["status", "lease_expires_at"])
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("connector", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=255), nullable=False),
        sa.Column("thread", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbox_pending", "outbox", ["connector", "status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_outbox_pending", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_tasks_lease_expiry", table_name="tasks")
    op.drop_index("ix_tasks_session_status", table_name="tasks")
    op.drop_index("ix_tasks_queue", table_name="tasks")
    op.drop_table("tasks")
    op.drop_table("sessions")
