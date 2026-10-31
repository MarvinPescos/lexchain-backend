"""feat(ecopy): approved e-copy requests for finalized documents

A party asks for the unwatermarked signed PDF; the owner approves or declines,
and the approved copy is emailed. ecopy_requests holds the decision and the
delivery state apart; ecopy_events is the audit trail. One PENDING request per
client and document, enforced by a partial unique index.

Revision ID: b3d7e9f15a26
Revises: a9c3e5f17b20
Create Date: 2026-10-31 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b3d7e9f15a26'
down_revision: str | Sequence[str] | None = 'a9c3e5f17b20'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'ecopy_requests',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('document_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('client_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decided_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('decline_reason', sa.Text(), nullable=True),
        sa.Column('approved_copy_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('approved_sha256', sa.String(length=64), nullable=True),
        sa.Column('delivery_status', sa.String(length=20), nullable=True),
        sa.Column('delivery_attempts', sa.Integer(), nullable=False),
        sa.Column('delivery_started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delivered_to', sa.String(length=255), nullable=True),
        sa.Column('delivery_error', sa.String(length=40), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['client_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['decided_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['approved_copy_id'], ['signed_copies.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ecopy_requests_document_id'), 'ecopy_requests', ['document_id'])
    op.create_index(op.f('ix_ecopy_requests_client_id'), 'ecopy_requests', ['client_id'])
    op.create_index(
        'uq_ecopy_request_pending',
        'ecopy_requests',
        ['document_id', 'client_id'],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_table(
        'ecopy_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('request_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('action', sa.String(length=30), nullable=False),
        sa.Column('actor_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('detail', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
        sa.ForeignKeyConstraint(['request_id'], ['ecopy_requests.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_ecopy_events_request_id'), 'ecopy_events', ['request_id'])
    # Stored by enum name, like the existing values.
    for value in (
        "ECOPY_REQUESTED",
        "ECOPY_APPROVED",
        "ECOPY_DECLINED",
        "ECOPY_DELIVERY_FAILED",
    ):
        op.execute(f"ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Downgrade schema.

    The notification enum values stay: Postgres cannot drop an enum value.
    """
    op.drop_index(op.f('ix_ecopy_events_request_id'), table_name='ecopy_events')
    op.drop_table('ecopy_events')
    op.drop_index('uq_ecopy_request_pending', table_name='ecopy_requests')
    op.drop_index(op.f('ix_ecopy_requests_client_id'), table_name='ecopy_requests')
    op.drop_index(op.f('ix_ecopy_requests_document_id'), table_name='ecopy_requests')
    op.drop_table('ecopy_requests')
