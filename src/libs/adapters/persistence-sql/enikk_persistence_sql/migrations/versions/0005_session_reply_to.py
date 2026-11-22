"""Delegated sessions answer in the thread they were delegated from (ADR-0022).

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("reply_channel", sa.String(length=255), nullable=True))
    op.add_column("sessions", sa.Column("reply_thread", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("sessions", "reply_thread")
    op.drop_column("sessions", "reply_channel")
