"""feat(backup): record where each finalized snapshot was archived

The lifecycle rewrite dropped `documents.backup_key` and stopped archiving.
The archive belongs to the frozen copy, so it is recorded on the snapshot.
Snapshots finalized before this stay NULL; backup verification reports them
as missing and can archive them from the live copy.

Revision ID: d3f1a9b7c2e4
Revises: c1e8a7d30f64
Create Date: 2026-10-21 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd3f1a9b7c2e4'
down_revision: str | Sequence[str] | None = 'c1e8a7d30f64'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'document_snapshots',
        sa.Column('backup_key', sa.String(length=500), nullable=True),
    )
    op.add_column(
        'document_snapshots',
        sa.Column('backed_up_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('document_snapshots', 'backed_up_at')
    op.drop_column('document_snapshots', 'backup_key')
