"""feat(records): anchor the analysis record

Revision ID: b8f3a2d61c47
Revises: a4e1d7c2b906
Create Date: 2026-11-08 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8f3a2d61c47'
down_revision: str | Sequence[str] | None = 'a4e1d7c2b906'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'document_snapshots', sa.Column('analysis_sha256', sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('document_snapshots', 'analysis_sha256')
