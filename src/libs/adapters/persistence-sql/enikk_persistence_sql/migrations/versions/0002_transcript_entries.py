"""Engine transcript entries (ADR-0014).

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_KEY = ["session_id", "project_key", "engine_session_id", "subpath"]


def upgrade() -> None:
    op.create_table(
        "transcript_entries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("project_key", sa.String(length=255), nullable=False),
        sa.Column("engine_session_id", sa.String(length=255), nullable=False),
        sa.Column("subpath", sa.String(length=255), nullable=False),
        sa.Column("entry_uuid", sa.String(length=255), nullable=True),
        sa.Column("entry", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_transcript_entries_key", "transcript_entries", [*_KEY, "id"])
    op.create_index(
        "uq_transcript_entries_uuid", "transcript_entries", [*_KEY, "entry_uuid"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_transcript_entries_uuid", table_name="transcript_entries")
    op.drop_index("ix_transcript_entries_key", table_name="transcript_entries")
    op.drop_table("transcript_entries")
