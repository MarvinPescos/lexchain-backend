"""feat(blockchain): notify lawyers of an anchored record missing from the database

Revision ID: d8f2b6c40e13
Revises: c4e8a2d61f37
Create Date: 2026-11-05 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd8f2b6c40e13'
down_revision: str | Sequence[str] | None = 'c4e8a2d61f37'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Stored by enum name, like the existing values.
    op.execute("ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS 'ANCHORED_RECORD_MISSING'")


def downgrade() -> None:
    """Downgrade schema.

    The notification enum value stays: Postgres cannot drop an enum value.
    """
