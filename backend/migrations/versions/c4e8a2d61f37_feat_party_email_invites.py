"""feat(sharing): invite a party who has no account yet

document_parties.user_id becomes nullable: an invitation to an address with
no account is held by email and claimed at the invitee's first verified
sign-in. One open email invitation per address per document. An email
invitation lapses after `invite_expires_at` unless the owner resends it.

Revision ID: c4e8a2d61f37
Revises: d2f8a61c9b47
Create Date: 2026-11-03 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4e8a2d61f37'
down_revision: str | Sequence[str] | None = 'd2f8a61c9b47'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('document_parties', 'user_id', nullable=True)
    op.add_column(
        'document_parties',
        sa.Column('invite_expires_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        'uq_document_party_email_invite',
        'document_parties',
        ['document_id', sa.text('lower(email)')],
        unique=True,
        postgresql_where=sa.text('user_id IS NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_document_party_email_invite', table_name='document_parties')
    op.drop_column('document_parties', 'invite_expires_at')
    # Unclaimed email invitations have no user to point at.
    op.execute('DELETE FROM document_parties WHERE user_id IS NULL')
    op.alter_column('document_parties', 'user_id', nullable=False)
