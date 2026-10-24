"""feat(restore): record the hashes a restore swapped on the stage change

A restore keeps the document FINALIZED, so the stage change alone does not say
what happened. `from_hash` is the file found in place of the signed copy,
`to_hash` the archived bytes put back. NULL for every other action.

Revision ID: e7a2c4d91f35
Revises: d3f1a9b7c2e4
Create Date: 2026-10-24 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e7a2c4d91f35'
down_revision: str | Sequence[str] | None = 'd3f1a9b7c2e4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'document_stage_changes',
        sa.Column('from_hash', sa.String(length=64), nullable=True),
    )
    op.add_column(
        'document_stage_changes',
        sa.Column('to_hash', sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('document_stage_changes', 'to_hash')
    op.drop_column('document_stage_changes', 'from_hash')
