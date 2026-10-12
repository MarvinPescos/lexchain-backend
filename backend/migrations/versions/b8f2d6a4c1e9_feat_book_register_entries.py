"""feat(book register entries)

Books become registers rather than counters:

  books
    - drop document_count, page_count, is_full   (counted from entries now)
    + status OPEN | CLOSED, closed_at             (full books backfill CLOSED)
    + uq_book_user_number_year                    one book per owner/number/year
    + uq_book_one_open_per_year                   at most one OPEN book per owner/year

  documents
    + doc_no, page_no                             the register entry (Doc. No., Page No.)
    + uq_document_register_entry                  one latest document per Doc. No. in a book

Existing documents are numbered in upload order per book (5 per page), each
version chain sharing its first version's entry. Existing duplicate books, or
two open books in one year, stop the upgrade with the rows listed — which book
survives is a human decision.

Revision ID: b8f2d6a4c1e9
Revises: a1d73f5c8e92
Create Date: 2026-09-24 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b8f2d6a4c1e9'
down_revision: Union[str, Sequence[str], None] = 'a1d73f5c8e92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _abort_if_rows(query: str, problem: str) -> None:
    rows = op.get_bind().execute(sa.text(query)).fetchall()
    if rows:
        listed = "\n".join(f"  {tuple(r)}" for r in rows)
        raise RuntimeError(
            f"Cannot add book constraints: {problem}. Resolve these rows "
            f"(merge, renumber or close the books) and re-run the migration:\n{listed}"
        )


def upgrade() -> None:
    """Upgrade schema."""
    # === books ===
    op.add_column(
        'books',
        sa.Column('status', sa.String(length=20), nullable=False,
                  server_default='OPEN'),
    )
    op.add_column(
        'books', sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "UPDATE books SET status = 'CLOSED', closed_at = now() WHERE is_full"
    )

    _abort_if_rows(
        """
        SELECT user_id, book_number, series_year, count(*)
        FROM books
        GROUP BY user_id, book_number, series_year
        HAVING count(*) > 1
        """,
        "duplicate (user_id, book_number, series_year)",
    )
    _abort_if_rows(
        """
        SELECT user_id, series_year, count(*)
        FROM books
        WHERE status = 'OPEN'
        GROUP BY user_id, series_year
        HAVING count(*) > 1
        """,
        "more than one OPEN book for the same owner and year",
    )

    op.create_unique_constraint(
        'uq_book_user_number_year',
        'books',
        ['user_id', 'book_number', 'series_year'],
    )
    op.create_index(
        'uq_book_one_open_per_year',
        'books',
        ['user_id', 'series_year'],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )

    op.drop_column('books', 'is_full')
    op.drop_column('books', 'page_count')
    op.drop_column('books', 'document_count')

    # === documents ===
    op.add_column('documents', sa.Column('doc_no', sa.Integer(), nullable=True))
    op.add_column('documents', sa.Column('page_no', sa.Integer(), nullable=True))

    # Number each chain once, in upload order within its book; every version
    # of a chain gets its root's entry.
    op.execute(
        """
        WITH roots AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY book_id ORDER BY created_at, id
                   ) AS n
            FROM documents
            WHERE id = coalesce(root_document_id, id)
        )
        UPDATE documents d
        SET doc_no = r.n,
            page_no = (r.n - 1) / 5 + 1
        FROM roots r
        WHERE coalesce(d.root_document_id, d.id) = r.id
        """
    )

    op.alter_column('documents', 'doc_no', nullable=False)
    op.alter_column('documents', 'page_no', nullable=False)
    op.create_index(
        'uq_document_register_entry',
        'documents',
        ['book_id', 'doc_no'],
        unique=True,
        postgresql_where=sa.text('is_latest'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_document_register_entry', table_name='documents')
    op.drop_column('documents', 'page_no')
    op.drop_column('documents', 'doc_no')

    op.add_column(
        'books',
        sa.Column('document_count', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'books',
        sa.Column('page_count', sa.Integer(), nullable=False, server_default='1'),
    )
    op.add_column(
        'books',
        sa.Column('is_full', sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute("UPDATE books SET is_full = (status = 'CLOSED')")

    op.drop_index('uq_book_one_open_per_year', table_name='books')
    op.drop_constraint('uq_book_user_number_year', 'books', type_='unique')
    op.drop_column('books', 'closed_at')
    op.drop_column('books', 'status')
