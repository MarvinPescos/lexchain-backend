"""feat(freeze block layout in the snapshot)

Stores the approved block layout alongside the attested text so a tamper diff
can point at a region of the page, not just a line number.

Only `text` is hashed and anchored — `blocks` is supporting evidence of where
that text sat, and is verified indirectly because its text must reconstruct the
hashed original.

Existing snapshots keep `blocks = NULL`; verification falls back to the
text-only diff for those and reports `localized: false`.

Revision ID: e5b21d9f8a44
Revises: d4e8a91c72b6
Create Date: 2026-07-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e5b21d9f8a44'
down_revision: Union[str, Sequence[str], None] = 'd4e8a91c72b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'document_snapshots',
        sa.Column('blocks', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('document_snapshots', 'blocks')
