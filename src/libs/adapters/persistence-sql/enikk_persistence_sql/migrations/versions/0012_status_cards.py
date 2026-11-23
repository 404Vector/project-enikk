"""The status card of each agent PR in the thread that asked for it (ADR-0036).

Expand only (I-34): a new table that older code does not read.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "status_cards",
        sa.Column("repository", sa.String(length=200), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=255), nullable=False),
        sa.Column("ts", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("repository", "number"),
    )


def downgrade() -> None:
    op.drop_table("status_cards")
