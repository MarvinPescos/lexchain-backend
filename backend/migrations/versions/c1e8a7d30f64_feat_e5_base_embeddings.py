"""feat(retrieval): e5-base embeddings (768-dim)

The chunk vector column widens from 384 (all-MiniLM-L6-v2) to 768
(intfloat/e5-base-v2). Vectors from the old model live in a different space
and cannot be compared with e5 query vectors, so they are cleared; run the
`document.reembed_chunks` task afterwards to rebuild them from chunk text.

Revision ID: c1e8a7d30f64
Revises: b9d4f6a20c53
Create Date: 2026-10-20 00:00:00.000000

"""
from collections.abc import Sequence

import pgvector.sqlalchemy
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c1e8a7d30f64'
down_revision: str | Sequence[str] | None = 'b9d4f6a20c53'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("UPDATE document_chunks SET embedding = NULL, embedding_vec = NULL")
    op.alter_column(
        'document_chunks',
        'embedding_vec',
        type_=pgvector.sqlalchemy.vector.VECTOR(dim=768),
        existing_nullable=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("UPDATE document_chunks SET embedding = NULL, embedding_vec = NULL")
    op.alter_column(
        'document_chunks',
        'embedding_vec',
        type_=pgvector.sqlalchemy.vector.VECTOR(dim=384),
        existing_nullable=True,
    )
