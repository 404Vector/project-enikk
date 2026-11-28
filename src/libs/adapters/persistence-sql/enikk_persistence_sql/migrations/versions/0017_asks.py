"""Requests of one agent for another's work, decided by the owner (ADR-0054).

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
        "asks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("requester", sa.String(length=63), nullable=False),
        sa.Column("agent", sa.String(length=63), nullable=False),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.Column("queued_task_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_asks_open", "asks", ["status", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_asks_open", table_name="asks")
    op.drop_table("asks")
