import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.database import Base, TimestampMixin, UserOwned
from app.shared.constants import PARTY_STATUS_PENDING

from .constants import LIFECYCLE_CANCELLED, LIFECYCLE_PREPARING

if TYPE_CHECKING:
    from app.features.blockchain.models import OnChainRecord
    from app.features.books import Book
    from app.features.chunks import DocumentChunk
    from app.features.documents.extraction.models import DocumentExtraction
    from app.features.insights.models import DocumentInsight
    from app.features.snapshot.models import DocumentSnapshot
    from app.features.users.models import User


UQ_DOCUMENT_REGISTER_ENTRY = "uq_document_register_entry"
UQ_DOCUMENT_SIGNED_FILE = "uq_document_signed_file"


class Document(UserOwned, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # user_id comes from UserOwned.
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Working draft in Google Docs (Path 1). Reference only — never stored,
    # hashed or processed; the signed PDF is the legal document.
    draft_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Drive file id of the draft, for sharing and locking it.
    draft_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Last time the draft's comments were mirrored. Null means never: the
    # first sync imports existing comments without notifying anyone.
    comments_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # === Signed copy ===
    # The copy finalize will freeze. Null until a signed PDF is attached
    # (Path 1) or uploaded (Path 2). Every upload is its own SignedCopy row, so
    # replacing the copy never loses the previous one.
    signed_copy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "signed_copies.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_documents_signed_copy_id",
        ),
        nullable=True,
    )
    # SHA-256 of the current signed copy, copied from it so the duplicate-file
    # rule can be a unique index. This is the hash anchored at finalize.
    document_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Pipeline status. Null until the document is finalized.
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    on_chain: Mapped[bool] = mapped_column(Boolean, default=False)

    # === Finalization ===
    # Only set once the document is snapshotted and anchored on-chain.
    finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finalized_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Changed only by lifecycle.transition — see constants for the stages.
    lifecycle: Mapped[str] = mapped_column(
        String(20), nullable=False, default=LIFECYCLE_PREPARING
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # === Register entry ===
    # "Doc. No. 45; Page No. 9; Book No. 2; Series of 2026" — written in the
    # notarial block at signing, so it is set when the signed copy is filed.
    # Null for a Path 1 document still being prepared.
    book_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("books.id", ondelete="CASCADE"),
        nullable=True,
    )
    doc_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # === relationship ===
    # String form, not [user_id]: the column now lives on UserOwned, so the
    # bare name is not in this class body. Document has a second FK to users.id
    # (finalized_by), so the relationship still has to say which one it means.
    user: Mapped["User"] = relationship(
        back_populates="documents", foreign_keys="[Document.user_id]"
    )
    book: Mapped["Book | None"] = relationship(back_populates="documents")
    signed_copy: Mapped["SignedCopy | None"] = relationship(
        foreign_keys=[signed_copy_id], post_update=True
    )
    signed_copies: Mapped[list["SignedCopy"]] = relationship(
        back_populates="document",
        foreign_keys="[SignedCopy.document_id]",
        cascade="all, delete-orphan",
        order_by="SignedCopy.created_at.desc()",
    )
    extractions: Mapped[list["DocumentExtraction"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )
    insights: Mapped[list["DocumentInsight"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
    )
    on_chain_record: Mapped["OnChainRecord | None"] = relationship(
        back_populates="document",
        uselist=False,
    )
    parties: Mapped[list["DocumentParty"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    snapshot: Mapped["DocumentSnapshot | None"] = relationship(
        back_populates="document"
    )
    stage_changes: Mapped[list["DocumentStageChange"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DocumentStageChange.created_at",
    )
    draft_comments: Mapped[list["DraftComment"]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="DraftComment.remote_created_at",
    )

    __table_args__ = (
        # The register entry is all or nothing.
        CheckConstraint(
            "(book_id IS NULL AND doc_no IS NULL AND page_no IS NULL) OR "
            "(book_id IS NOT NULL AND doc_no IS NOT NULL AND page_no IS NOT NULL)",
            name="ck_document_register_entry_complete",
        ),
        # One document per Doc. No. in a book. Documents not filed yet (no
        # book) don't take part.
        Index(
            UQ_DOCUMENT_REGISTER_ENTRY,
            "book_id",
            "doc_no",
            unique=True,
            postgresql_where=text("book_id IS NOT NULL"),
        ),
        # The same signed file cannot be filed twice by one lawyer. Only the
        # current copy counts: a file replaced on one document can still be
        # filed under the document it belongs to.
        Index(
            UQ_DOCUMENT_SIGNED_FILE,
            "user_id",
            "document_hash",
            unique=True,
            postgresql_where=text(
                f"document_hash IS NOT NULL AND lifecycle <> '{LIFECYCLE_CANCELLED}'"
            ),
        ),
    )


class SignedCopy(Base, TimestampMixin):
    """One uploaded signed/notarized PDF. Never updated; a replacement is a new row."""

    __tablename__ = "signed_copies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    storage_url: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(String(50), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Set on the copy that replaced another — why the earlier one was wrong.
    replaced_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # clock_timestamp, not now(): now() is fixed per transaction, and copies
    # are listed in upload order.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        default=func.clock_timestamp(),
    )

    document: Mapped["Document"] = relationship(
        back_populates="signed_copies", foreign_keys=[document_id]
    )


class DocumentStageChange(Base):
    """Audit trail: one row per lifecycle action, written by lifecycle.transition."""

    __tablename__ = "document_stage_changes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_stage: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_stage: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.clock_timestamp(),
        default=func.clock_timestamp(),
    )

    document: Mapped["Document"] = relationship(back_populates="stage_changes")


class DraftComment(Base, TimestampMixin):
    """A comment thread on the working draft, mirrored from Google Docs.

    Kept after the draft is locked, so the discussion that led to the signed
    text stays on record. Authors are LexChain users when they can be matched
    (the lawyer, or a party via their Drive permission); `author_name` is
    what Drive showed either way.
    """

    __tablename__ = "draft_comments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    author_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    quoted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    remote_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    document: Mapped["Document"] = relationship(back_populates="draft_comments")
    replies: Mapped[list["DraftReply"]] = relationship(
        back_populates="comment",
        cascade="all, delete-orphan",
        order_by="DraftReply.remote_created_at",
    )

    __table_args__ = (
        UniqueConstraint("document_id", "external_id", name="uq_draft_comment"),
    )


class DraftReply(Base, TimestampMixin):
    """A reply in a draft comment thread. Resolving and reopening are replies
    too (`action`), so who did it is always known."""

    __tablename__ = "draft_replies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    comment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("draft_comments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    author_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    action: Mapped[str | None] = mapped_column(String(10), nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    remote_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    comment: Mapped["DraftComment"] = relationship(back_populates="replies")

    __table_args__ = (
        UniqueConstraint("comment_id", "external_id", name="uq_draft_reply"),
    )


# ========


class DocumentParty(Base, TimestampMixin):
    __tablename__ = "document_parties"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), default="viewer")
    # An invitation is not access. Until the invitee accepts, this row grants
    # nothing and the document does not appear in their list.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PARTY_STATUS_PENDING
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The Drive permission giving this party access to the working draft.
    # Set once they accept and the document has a draft.
    draft_permission_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # relasyon haha
    document: Mapped["Document"] = relationship(back_populates="parties")
    user: Mapped["User"] = relationship(back_populates="parties")

    __table_args__ = (
        UniqueConstraint("document_id", "user_id", name="uq_document_party"),
        # Every access check and the pending-invitation list filter on this pair.
        Index("ix_document_parties_user_status", "user_id", "status"),
    )
