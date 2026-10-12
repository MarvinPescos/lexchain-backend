"""feat(track document archive location)

Records where a finalized document's original PDF was archived, so it can be
restored after tampering or accidental deletion.

Revision ID: a1d73f5c8e92
Revises: f6c94e2a1b78
Create Date: 2026-08-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a1d73f5c8e92'
down_revision: Union[str, Sequence[str], None] = 'f6c94e2a1b78'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'documents',
        sa.Column('backed_up_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'documents', sa.Column('backup_key', sa.String(length=500), nullable=True)
    )
    # Documents finalized before backups existed have no archive. Left NULL on
    # purpose so the coverage query surfaces them as gaps rather than pretending
    # they are recoverable.


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('documents', 'backup_key')
    op.drop_column('documents', 'backed_up_at')
