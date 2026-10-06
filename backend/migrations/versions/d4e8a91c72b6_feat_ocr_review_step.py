"""feat(add issuer OCR review step)

Adds the review columns to `document_extraction`. `text` stays the untouched
OCR output; the issuer's corrections live in `blocks` / `reviewed_text` so a
later dispute can show exactly what a human changed and what the machine read.

Revision ID: d4e8a91c72b6
Revises: c7a1f2b48d33
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd4e8a91c72b6'
down_revision: Union[str, Sequence[str], None] = 'c7a1f2b48d33'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'document_extraction',
        sa.Column('blocks', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'document_extraction',
        sa.Column('flags', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        'document_extraction', sa.Column('reviewed_text', sa.Text(), nullable=True)
    )
    op.add_column(
        'document_extraction', sa.Column('reviewed_by', sa.UUID(), nullable=True)
    )
    op.add_column(
        'document_extraction',
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'document_extraction',
        sa.Column(
            'edited_block_count', sa.Integer(), nullable=False, server_default='0'
        ),
    )
    op.alter_column('document_extraction', 'edited_block_count', server_default=None)

    op.create_foreign_key(
        'document_extraction_reviewed_by_fkey',
        'document_extraction', 'users',
        ['reviewed_by'], ['id'],
        ondelete='SET NULL',
    )

    # Documents already processed under the old single-phase pipeline were never
    # reviewed. Treat their OCR text as approved so they keep working, rather
    # than stranding them in a review that nobody was asked to do.
    op.execute(
        """
        UPDATE document_extraction
        SET reviewed_text = text
        WHERE reviewed_text IS NULL
          AND document_id IN (SELECT id FROM documents WHERE status = 'COMPLETED')
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        'document_extraction_reviewed_by_fkey',
        'document_extraction',
        type_='foreignkey',
    )
    op.drop_column('document_extraction', 'edited_block_count')
    op.drop_column('document_extraction', 'reviewed_at')
    op.drop_column('document_extraction', 'reviewed_by')
    op.drop_column('document_extraction', 'reviewed_text')
    op.drop_column('document_extraction', 'flags')
    op.drop_column('document_extraction', 'blocks')
