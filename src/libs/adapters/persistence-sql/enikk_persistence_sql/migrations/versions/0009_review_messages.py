"""The owner's DM message per pull request, updated in place (ADR-0020 §4).

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_messages",
        sa.Column("repository", sa.String(length=200), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=255), nullable=False),
        sa.Column("ts", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("repository", "number"),
    )


def downgrade() -> None:
    op.drop_table("review_messages")
