"""fix(documents): process signed copies before finalize

Processing now starts when a signed copy lands, and finalize waits for it to
complete. Documents already SIGNED were never queued, so each gets an extract
job in the outbox and is set QUEUED; the dispatch_pending beat task sends it.
Data only — no schema change.

Revision ID: d2f8a61c9b47
Revises: b3d7e9f15a26
Create Date: 2026-11-04 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd2f8a61c9b47'
down_revision: str | Sequence[str] | None = 'b3d7e9f15a26'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Queue every unprocessed SIGNED document."""
    op.execute(
        """
        INSERT INTO processing_dispatches (id, document_id, job, attempts, created_at)
        SELECT gen_random_uuid(), id, 'extract', 0, now() - interval '5 minutes'
        FROM documents
        WHERE lifecycle = 'SIGNED' AND status IS NULL AND signed_copy_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE documents SET status = 'QUEUED'
        WHERE lifecycle = 'SIGNED' AND status IS NULL AND signed_copy_id IS NOT NULL
        """
    )


def downgrade() -> None:
    """Nothing to undo: the queued jobs are ordinary processing."""
