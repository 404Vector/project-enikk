"""The owner's decisions on publishing, unpublishing, deleting or moving notes
(ADR-0051 §4).

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "note_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("target", sa.String(length=500), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("blob_sha", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("note_changes")
