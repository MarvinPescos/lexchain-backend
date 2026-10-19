"""feat(documents): pending Google draft revocations

A removed party's Drive permission is recorded here in the same commit that
removes the party, and deleted only once Drive confirms it is gone. Before,
a failed revocation was logged and the permission id was lost with the party
row, so the removed party could keep access to the working draft.

Revision ID: a7c3e5f19b42
Revises: e23918dc9d39
Create Date: 2026-10-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a7c3e5f19b42'
down_revision: Union[str, Sequence[str], None] = 'e23918dc9d39'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('draft_revocations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('draft_file_id', sa.String(length=128), nullable=False),
    sa.Column('permission_id', sa.String(length=128), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('draft_file_id', 'permission_id', name='uq_draft_revocation_permission')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('draft_revocations')
