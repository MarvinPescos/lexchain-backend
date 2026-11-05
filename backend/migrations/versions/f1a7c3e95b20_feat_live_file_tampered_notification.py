"""feat(snapshot): notify the lawyer of a tampered live file

Revision ID: f1a7c3e95b20
Revises: d8f2b6c40e13
Create Date: 2026-11-05 12:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f1a7c3e95b20'
down_revision: str | Sequence[str] | None = 'd8f2b6c40e13'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Stored by enum name, like the existing values.
    op.execute("ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS 'LIVE_FILE_TAMPERED'")


def downgrade() -> None:
    """Downgrade schema.

    The notification enum value stays: Postgres cannot drop an enum value.
    """
