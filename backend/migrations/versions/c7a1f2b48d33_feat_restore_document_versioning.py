"""feat(restore document versioning)

Brings back the version chain on `documents` (version / is_latest /
root_document_id / previous_document_id) alongside the lifecycle + snapshot
columns added in b5e3c4d21e1c.

Also makes `finalized_at` nullable — it is only set when a document is actually
finalized, so defaulting it to now() on every draft was wrong.

Revision ID: c7a1f2b48d33
Revises: b5e3c4d21e1c
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c7a1f2b48d33'
down_revision: Union[str, Sequence[str], None] = 'b5e3c4d21e1c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ── Versioning columns ──
    # server_default backfills existing rows, then is dropped so the ORM default owns it.
    op.add_column(
        'documents',
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
    )
    op.add_column(
        'documents',
        sa.Column('is_latest', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column('documents', sa.Column('root_document_id', sa.UUID(), nullable=True))
    op.add_column(
        'documents', sa.Column('previous_document_id', sa.UUID(), nullable=True)
    )

    op.create_foreign_key(
        'documents_root_document_id_fkey',
        'documents', 'documents',
        ['root_document_id'], ['id'],
        ondelete='SET NULL',
    )
    op.create_foreign_key(
        'documents_previous_document_id_fkey',
        'documents', 'documents',
        ['previous_document_id'], ['id'],
        ondelete='SET NULL',
    )

    # Existing rows are all standalone v1 documents: each is its own chain root.
    op.execute("UPDATE documents SET root_document_id = id WHERE root_document_id IS NULL")

    op.alter_column('documents', 'version', server_default=None)
    op.alter_column('documents', 'is_latest', server_default=None)

    op.create_index(
        'ix_documents_root_document_id', 'documents', ['root_document_id']
    )
    op.create_unique_constraint(
        'uq_document_version', 'documents', ['root_document_id', 'version']
    )

    # ── Lifecycle ──
    op.execute("UPDATE documents SET lifecycle = 'DRAFT' WHERE lifecycle IS NULL")
    op.alter_column(
        'documents', 'lifecycle',
        existing_type=sa.String(length=20),
        nullable=False,
    )

    # ── finalized_at only means something once finalized ──
    # Drop NOT NULL before clearing the values: b5e3c4d21e1c created this column
    # as NOT NULL DEFAULT now(), so nulling existing rows first would violate the
    # constraint on any database that already holds documents.
    op.alter_column(
        'documents', 'finalized_at',
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
        server_default=None,
    )
    op.execute("UPDATE documents SET finalized_at = NULL WHERE lifecycle <> 'FINALIZED'")


def downgrade() -> None:
    """Downgrade schema."""
    # Restoring NOT NULL requires every row to hold a value first: the upgrade
    # cleared finalized_at for everything that was never actually finalized.
    op.execute("UPDATE documents SET finalized_at = now() WHERE finalized_at IS NULL")
    op.alter_column(
        'documents', 'finalized_at',
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text('now()'),
    )
    op.alter_column(
        'documents', 'lifecycle',
        existing_type=sa.String(length=20),
        nullable=True,
    )

    op.drop_constraint('uq_document_version', 'documents', type_='unique')
    op.drop_index('ix_documents_root_document_id', table_name='documents')
    op.drop_constraint(
        'documents_previous_document_id_fkey', 'documents', type_='foreignkey'
    )
    op.drop_constraint(
        'documents_root_document_id_fkey', 'documents', type_='foreignkey'
    )
    op.drop_column('documents', 'previous_document_id')
    op.drop_column('documents', 'root_document_id')
    op.drop_column('documents', 'is_latest')
    op.drop_column('documents', 'version')
