"""feat(documents): processing dispatch outbox

Finalize and approval now record the OCR / enrichment job in the same commit
as their state change, and the job is sent to the broker afterwards. Pending
rows are resent by the `document.dispatch_pending` beat task, so a broker
outage delays processing instead of stranding the document.

Revision ID: b9d4f6a20c53
Revises: a7c3e5f19b42
Create Date: 2026-10-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b9d4f6a20c53'
down_revision: Union[str, Sequence[str], None] = 'a7c3e5f19b42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('processing_dispatches',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('job', sa.String(length=20), nullable=False),
    sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_processing_dispatches_pending',
        'processing_dispatches',
        ['created_at'],
        postgresql_where=sa.text('dispatched_at IS NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_processing_dispatches_pending', table_name='processing_dispatches')
    op.drop_table('processing_dispatches')
