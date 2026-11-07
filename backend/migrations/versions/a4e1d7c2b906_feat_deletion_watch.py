"""feat(records): save a deleted document and alert on it

Revision ID: a4e1d7c2b906
Revises: c7d2e9f04a31
Create Date: 2026-11-06 14:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a4e1d7c2b906'
down_revision: str | Sequence[str] | None = 'c7d2e9f04a31'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# BEFORE DELETE, so the child rows are still there to copy. The table list must
# match `RECOVERED_TABLES`. A row-level trigger doesn't fire on TRUNCATE or under
# session_replication_role = replica, so a dump load and the RDS copy skip it.
CREATE_FUNCTION = """
CREATE FUNCTION capture_deleted_document() RETURNS trigger
LANGUAGE plpgsql
SET search_path = public
AS $fn$
DECLARE
    tbl text;
    found jsonb;
    related jsonb := '{}'::jsonb;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'signed_copies', 'document_stage_changes', 'document_parties',
        'draft_comments', 'document_extraction', 'document_chunks',
        'document_insight', 'draft_revocations', 'processing_dispatches',
        'ecopy_requests', 'on_chain_records', 'verification_results'
    ] LOOP
        EXECUTE format(
            'SELECT coalesce(jsonb_agg(to_jsonb(t)), ''[]''::jsonb) '
            'FROM %I t WHERE t.document_id = $1',
            tbl
        ) INTO found USING OLD.id;
        related := related || jsonb_build_object(tbl, found);
    END LOOP;

    SELECT coalesce(jsonb_agg(to_jsonb(r)), '[]'::jsonb) INTO found
    FROM draft_replies r
    JOIN draft_comments c ON c.id = r.comment_id
    WHERE c.document_id = OLD.id;
    related := related || jsonb_build_object('draft_replies', found);

    SELECT coalesce(jsonb_agg(to_jsonb(e)), '[]'::jsonb) INTO found
    FROM ecopy_events e
    JOIN ecopy_requests q ON q.id = e.request_id
    WHERE q.document_id = OLD.id;
    related := related || jsonb_build_object('ecopy_events', found);

    INSERT INTO deleted_documents (
        document_id, user_id, file_name, source, document, related, cascaded,
        deleted_by
    ) VALUES (
        OLD.id, OLD.user_id, OLD.file_name, 'trigger', to_jsonb(OLD), related,
        pg_trigger_depth() > 1, current_user
    );
    RETURN OLD;
END
$fn$
"""

CREATE_TRIGGER = """
CREATE TRIGGER capture_deleted_document
BEFORE DELETE ON documents
FOR EACH ROW EXECUTE FUNCTION capture_deleted_document()
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'deleted_documents',
        sa.Column(
            'id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False
        ),
        sa.Column('document_id', sa.UUID(), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=True),
        sa.Column('file_name', sa.String(length=255), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('document', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('related', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('cascaded', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('deleted_by', sa.String(length=63), nullable=True),
        sa.Column(
            'deleted_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.clock_timestamp(),
            nullable=False,
        ),
        sa.Column('alerted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('recovered_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_deleted_documents_document_id'), 'deleted_documents', ['document_id']
    )
    op.execute(CREATE_FUNCTION)
    op.execute(CREATE_TRIGGER)
    # Stored by enum name, like the existing values.
    op.execute("ALTER TYPE notificationtype ADD VALUE IF NOT EXISTS 'DOCUMENT_DELETED'")


def downgrade() -> None:
    """Downgrade schema.

    The notification enum value stays: Postgres cannot drop an enum value.
    """
    op.execute('DROP TRIGGER capture_deleted_document ON documents')
    op.execute('DROP FUNCTION capture_deleted_document()')
    op.drop_index(op.f('ix_deleted_documents_document_id'), table_name='deleted_documents')
    op.drop_table('deleted_documents')
