"""feat(document_chunks): add embedding_vec

Revision ID: fb63723c5b37
Revises: 02fcd3118ced
Create Date: 2026-05-05 04:46:14.764020

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = 'fb63723c5b37'
down_revision: Union[str, Sequence[str], None] = '02fcd3118ced'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.add_column(
        "document_chunks",
        sa.Column("embedding_vec", Vector(384), nullable=True),
    )

    op.create_index(
        "ix_document_chunks_embedding_vec_cosine",
        "document_chunks",
        ["embedding_vec"],
        postgresql_using="ivfflat",
        postgresql_with={"lists": 100},
        postgresql_ops={"embedding_vec": "vector_cosine_ops"},
    )

def downgrade() -> None:
    op.drop_index(
        "ix_document_chunks_embedding_vec_cosine",
        table_name="document_chunks",
    )
    op.drop_column("document_chunks", "embedding_vec")