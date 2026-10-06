"""feat(documents): lifecycle stages, signed copies, stage history

One Document lifecycle with two entry points (PREPARING → READY_FOR_SIGNATURE
→ SIGNED, or straight to SIGNED), then FINALIZED. Signed PDFs move to their own
table so a replacement keeps the previous copy; every stage action is logged.
The version chain, the archive columns and the text hash are dropped: what is
anchored is now the SHA-256 of the final PDF.

Schema only. Existing documents cannot be converted (version chains share a
register entry; old snapshots hold a text hash, not a file hash), so the
upgrade refuses to run while any document exists — reset the dev database.

Revision ID: 86e74d999c7f
Revises: b8f2d6a4c1e9
Create Date: 2026-10-02 15:40:15.872333

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '86e74d999c7f'
down_revision: Union[str, Sequence[str], None] = 'b8f2d6a4c1e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _refuse_if_documents_exist(direction: str) -> None:
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM documents")).scalar()
    if count:
        raise RuntimeError(
            f"Cannot {direction} with {count} existing document(s): this migration "
            "changes the document model and does not convert data. Reset the dev "
            "database first."
        )


def upgrade() -> None:
    """Upgrade schema."""
    _refuse_if_documents_exist("upgrade")

    op.create_table('document_stage_changes',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('from_stage', sa.String(length=20), nullable=True),
    sa.Column('to_stage', sa.String(length=20), nullable=False),
    sa.Column('action', sa.String(length=40), nullable=False),
    sa.Column('actor_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_document_stage_changes_document_id'), 'document_stage_changes', ['document_id'], unique=False)
    op.create_table('signed_copies',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('storage_url', sa.String(length=500), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('content_type', sa.String(length=50), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('original_filename', sa.String(length=255), nullable=True),
    sa.Column('uploaded_by', sa.UUID(), nullable=True),
    sa.Column('replaced_reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('clock_timestamp()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['uploaded_by'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_signed_copies_document_id'), 'signed_copies', ['document_id'], unique=False)

    # Snapshot: the frozen signed copy and its file hash, not the OCR text.
    op.add_column('document_snapshots', sa.Column('signed_copy_id', sa.UUID(), nullable=False))
    op.add_column('document_snapshots', sa.Column('file_sha256', sa.String(length=64), nullable=False))
    op.add_column('document_snapshots', sa.Column('size_bytes', sa.Integer(), nullable=False))
    op.create_foreign_key('document_snapshots_signed_copy_id_fkey', 'document_snapshots', 'signed_copies', ['signed_copy_id'], ['id'], ondelete='RESTRICT')
    op.drop_column('document_snapshots', 'text_hash')
    op.drop_column('document_snapshots', 'text')
    op.drop_column('document_snapshots', 'blocks')

    # Documents: lifecycle stages, draft link, register entry set at filing.
    op.add_column('documents', sa.Column('draft_url', sa.String(length=500), nullable=True))
    op.add_column('documents', sa.Column('signed_copy_id', sa.UUID(), nullable=True))
    op.add_column('documents', sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True))
    op.alter_column('documents', 'document_hash',
               existing_type=sa.VARCHAR(length=500),
               type_=sa.String(length=64),
               nullable=True)
    op.alter_column('documents', 'status',
               existing_type=sa.VARCHAR(length=50),
               nullable=True)
    op.alter_column('documents', 'book_id',
               existing_type=sa.UUID(),
               nullable=True)
    op.alter_column('documents', 'doc_no',
               existing_type=sa.INTEGER(),
               nullable=True)
    op.alter_column('documents', 'page_no',
               existing_type=sa.INTEGER(),
               nullable=True)

    # The register index was partial on is_latest; it is now partial on filing.
    op.drop_index('uq_document_register_entry', table_name='documents', if_exists=True)
    op.drop_index(op.f('ix_documents_root_document_id'), table_name='documents')
    op.drop_constraint(op.f('uq_document_version'), 'documents', type_='unique')
    op.drop_constraint(op.f('documents_root_document_id_fkey'), 'documents', type_='foreignkey')
    op.drop_constraint(op.f('documents_previous_document_id_fkey'), 'documents', type_='foreignkey')
    op.drop_column('documents', 'backup_key')
    op.drop_column('documents', 'is_latest')
    op.drop_column('documents', 'root_document_id')
    op.drop_column('documents', 'previous_document_id')
    op.drop_column('documents', 'storage_url')
    op.drop_column('documents', 'version')
    op.drop_column('documents', 'content_hash')
    op.drop_column('documents', 'backed_up_at')
    op.drop_column('documents', 'content_type')

    op.alter_column('documents', 'lifecycle', server_default=None)
    op.create_index('uq_document_register_entry', 'documents', ['book_id', 'doc_no'], unique=True, postgresql_where=sa.text('book_id IS NOT NULL'))
    op.create_index('uq_document_signed_file', 'documents', ['user_id', 'document_hash'], unique=True, postgresql_where=sa.text("document_hash IS NOT NULL AND lifecycle <> 'CANCELLED'"))
    op.create_check_constraint(
        'ck_document_register_entry_complete',
        'documents',
        '(book_id IS NULL AND doc_no IS NULL AND page_no IS NULL) OR '
        '(book_id IS NOT NULL AND doc_no IS NOT NULL AND page_no IS NOT NULL)',
    )
    op.create_foreign_key('fk_documents_signed_copy_id', 'documents', 'signed_copies', ['signed_copy_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    """Downgrade schema."""
    _refuse_if_documents_exist("downgrade")

    op.drop_constraint('fk_documents_signed_copy_id', 'documents', type_='foreignkey')
    op.drop_constraint('ck_document_register_entry_complete', 'documents', type_='check')
    op.drop_index('uq_document_signed_file', table_name='documents')
    op.drop_index('uq_document_register_entry', table_name='documents')

    op.add_column('documents', sa.Column('content_type', sa.VARCHAR(length=50), autoincrement=False, nullable=False))
    op.add_column('documents', sa.Column('backed_up_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True))
    op.add_column('documents', sa.Column('content_hash', sa.VARCHAR(length=500), autoincrement=False, nullable=True))
    op.add_column('documents', sa.Column('version', sa.INTEGER(), autoincrement=False, nullable=False))
    op.add_column('documents', sa.Column('storage_url', sa.VARCHAR(length=500), autoincrement=False, nullable=False))
    op.add_column('documents', sa.Column('previous_document_id', sa.UUID(), autoincrement=False, nullable=True))
    op.add_column('documents', sa.Column('root_document_id', sa.UUID(), autoincrement=False, nullable=True))
    op.add_column('documents', sa.Column('is_latest', sa.BOOLEAN(), autoincrement=False, nullable=False))
    op.add_column('documents', sa.Column('backup_key', sa.VARCHAR(length=500), autoincrement=False, nullable=True))
    op.create_foreign_key(op.f('documents_previous_document_id_fkey'), 'documents', 'documents', ['previous_document_id'], ['id'], ondelete='SET NULL')
    op.create_foreign_key(op.f('documents_root_document_id_fkey'), 'documents', 'documents', ['root_document_id'], ['id'], ondelete='SET NULL')
    op.create_unique_constraint(op.f('uq_document_version'), 'documents', ['root_document_id', 'version'], postgresql_nulls_not_distinct=False)
    op.create_index(op.f('ix_documents_root_document_id'), 'documents', ['root_document_id'], unique=False)
    op.create_index('uq_document_register_entry', 'documents', ['book_id', 'doc_no'], unique=True, postgresql_where=sa.text('is_latest'))
    op.alter_column('documents', 'page_no',
               existing_type=sa.INTEGER(),
               nullable=False)
    op.alter_column('documents', 'doc_no',
               existing_type=sa.INTEGER(),
               nullable=False)
    op.alter_column('documents', 'book_id',
               existing_type=sa.UUID(),
               nullable=False)
    op.alter_column('documents', 'status',
               existing_type=sa.VARCHAR(length=50),
               nullable=False)
    op.alter_column('documents', 'document_hash',
               existing_type=sa.String(length=64),
               type_=sa.VARCHAR(length=500),
               nullable=False)
    op.drop_column('documents', 'cancelled_at')
    op.drop_column('documents', 'signed_copy_id')
    op.drop_column('documents', 'draft_url')

    op.add_column('document_snapshots', sa.Column('blocks', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True))
    op.add_column('document_snapshots', sa.Column('text', sa.TEXT(), autoincrement=False, nullable=False))
    op.add_column('document_snapshots', sa.Column('text_hash', sa.VARCHAR(length=64), autoincrement=False, nullable=False))
    op.drop_constraint('document_snapshots_signed_copy_id_fkey', 'document_snapshots', type_='foreignkey')
    op.drop_column('document_snapshots', 'size_bytes')
    op.drop_column('document_snapshots', 'file_sha256')
    op.drop_column('document_snapshots', 'signed_copy_id')
    op.drop_index(op.f('ix_signed_copies_document_id'), table_name='signed_copies')
    op.drop_table('signed_copies')
    op.drop_index(op.f('ix_document_stage_changes_document_id'), table_name='document_stage_changes')
    op.drop_table('document_stage_changes')
