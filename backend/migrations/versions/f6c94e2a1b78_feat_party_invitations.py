"""feat(document party invitations)

Adds an accept/decline step to document sharing. Being added to a document is
now an invitation, not access: the row starts `pending` and grants nothing until
the invitee accepts.

Existing parties are backfilled to `accepted` — they were granted access under
the old immediate-access behaviour, and revoking it silently would lock people
out of documents they can currently read.

Revision ID: f6c94e2a1b78
Revises: e5b21d9f8a44
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f6c94e2a1b78'
down_revision: Union[str, Sequence[str], None] = 'e5b21d9f8a44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default backfills existing rows as accepted, then is dropped so the
    # ORM default ('pending') governs every new invitation.
    op.add_column(
        'document_parties',
        sa.Column('status', sa.String(length=20), nullable=False,
                  server_default='accepted'),
    )
    op.add_column(
        'document_parties',
        sa.Column('responded_at', sa.DateTime(timezone=True), nullable=True),
    )

    # Existing parties already had access, so treat them as having accepted at
    # the time they were added.
    op.execute("UPDATE document_parties SET responded_at = created_at")

    op.alter_column('document_parties', 'status', server_default=None)

    op.create_index(
        'ix_document_parties_user_status',
        'document_parties',
        ['user_id', 'status'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_document_parties_user_status', table_name='document_parties')
    op.drop_column('document_parties', 'responded_at')
    op.drop_column('document_parties', 'status')
