"""Scheduled messages: an outbox message may wait until ``not_before`` (ADR-0042).

Expand only (I-34): a nullable column that older code does not read.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox", sa.Column("not_before", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("outbox", "not_before")
