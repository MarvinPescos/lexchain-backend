"""feat(verify): keep every verification for the integrity report

One row per completed check through GET /documents/{id}/verify, so a report
for a date range reads stored verdicts instead of re-verifying every file.
Indexed on (verified_at, id): the range filter and the stable page order.

Revision ID: a9c3e5f17b20
Revises: e7a2c4d91f35
Create Date: 2026-10-31 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a9c3e5f17b20'
down_revision: str | Sequence[str] | None = 'e7a2c4d91f35'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'verification_results',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('document_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('verified_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('is_authentic', sa.Boolean(), nullable=False),
        sa.Column('baseline_trusted', sa.Boolean(), nullable=False),
        sa.Column('onchain_hash', sa.String(length=500), nullable=True),
        sa.Column('snapshot_hash', sa.String(length=64), nullable=True),
        sa.Column('current_hash', sa.String(length=64), nullable=True),
        sa.Column('tx_hash', sa.String(length=66), nullable=True),
        sa.Column('tamper_report_status', sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['verified_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_verification_results_document_id'),
        'verification_results',
        ['document_id'],
    )
    op.create_index(
        'ix_verification_results_verified_at_id',
        'verification_results',
        ['verified_at', 'id'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_verification_results_verified_at_id', table_name='verification_results')
    op.drop_index(op.f('ix_verification_results_document_id'), table_name='verification_results')
    op.drop_table('verification_results')
