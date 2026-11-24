"""Schedules and their runs (ADR-0039 S1).

Expand only (I-34): new tables that older code does not read.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("owner_agent", sa.String(length=63), nullable=False),
        sa.Column("origin_connector", sa.String(length=32), nullable=False),
        sa.Column("origin_channel", sa.String(length=255), nullable=False),
        sa.Column("origin_thread", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_schedules_name"),
    )
    op.create_table(
        "schedule_runs",
        sa.Column("schedule_id", sa.Uuid(), nullable=False),
        sa.Column("slot", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("detail", sa.String(length=500), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["schedule_id"], ["schedules.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("schedule_id", "slot"),
    )
    op.create_index("ix_schedule_runs_status", "schedule_runs", ["status", "slot"])


def downgrade() -> None:
    op.drop_index("ix_schedule_runs_status", table_name="schedule_runs")
    op.drop_table("schedule_runs")
    op.drop_table("schedules")
