"""feat(records): anchor metadata, alert on record, archive and owner key findings

Revision ID: c7d2e9f04a31
Revises: f1a7c3e95b20
Create Date: 2026-11-06 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c7d2e9f04a31'
down_revision: str | Sequence[str] | None = 'f1a7c3e95b20'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'document_snapshots', sa.Column('metadata_sha256', sa.String(length=64), nullable=True)
    )
    op.add_column('document_stage_changes', sa.Column('changes', sa.JSON(), nullable=True))
    # Stored by enum name, like the existing values.
    op.execute("ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS 'RECORD_ALTERED'")
    op.execute("ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS 'ARCHIVE_PROBLEM'")
    op.execute("ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS 'ANCHOR_CORRECTED'")


def downgrade() -> None:
    """Downgrade schema.

    The notification enum values stay: Postgres cannot drop an enum value.
    """
    op.drop_column('document_stage_changes', 'changes')
    op.drop_column('document_snapshots', 'metadata_sha256')
