import uuid

from sqlalchemy import delete, or_, select

from app.infrastructure.database import UserScopeRepository
from app.shared.constants import (
    PARTY_STATUS_ACCEPTED,
    PARTY_STATUS_DECLINED,
    PARTY_STATUS_PENDING,
)

from .constants import LIFECYCLE_CANCELLED, LIFECYCLE_READY_FOR_SIGNATURE
from .models import (
    Document,
    DocumentParty,
    DocumentStageChange,
    DraftComment,
    DraftRevocation,
    SignedCopy,
)


class DocumentRepository(UserScopeRepository[Document]):
    """Repository pattern for Document"""

    def __init__(self, session):
        super().__init__(Document, session)

    # === Custom methods ===

    async def get_document_for_user(
        self,
        user_id: uuid.UUID,
        limit: int = 100,
        offset: int = 0,
        lifecycle: str | None = None,
    ) -> list[Document]:
        """Return document the user owns OR is a party of"""
        # A pending invitation is not access — it must be accepted first.
        shared_subq = select(DocumentParty.document_id).where(
            DocumentParty.user_id == user_id,
            DocumentParty.status == PARTY_STATUS_ACCEPTED,
        )

        stmt = (
            select(Document)
            .where(
                or_(Document.user_id == user_id, Document.id.in_(shared_subq)),
                *([Document.lifecycle == lifecycle] if lifecycle else []),
            )
            .order_by(Document.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        result = await self.session.execute(stmt)

        return list(result.scalars().all())

    async def find_current_signed_file(
        self, user_id: uuid.UUID, sha256: str
    ) -> Document | None:
        """A non-cancelled document of this lawyer whose current signed copy has this hash."""
        result = await self.session.execute(
            select(Document).where(
                Document.user_id == user_id,
                Document.document_hash == sha256,
                Document.lifecycle != LIFECYCLE_CANCELLED,
            )
        )
        return result.scalars().first()

    async def list_ready_for_signature(self, user_id: uuid.UUID) -> list[Document]:
        """Documents waiting for their signed copy, oldest first."""
        from sqlalchemy.orm import selectinload

        result = await self.session.execute(
            select(Document)
            .options(selectinload(Document.parties))
            .where(
                Document.user_id == user_id,
                Document.lifecycle == LIFECYCLE_READY_FOR_SIGNATURE,
            )
            .order_by(Document.created_at.asc())
        )
        return list(result.scalars().all())

    async def get_signed_copies(self, document_id: uuid.UUID) -> list[SignedCopy]:
        """Every signed copy uploaded for a document, newest first."""
        result = await self.session.execute(
            select(SignedCopy)
            .where(SignedCopy.document_id == document_id)
            .order_by(SignedCopy.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_stage_changes(
        self, document_id: uuid.UUID
    ) -> list[DocumentStageChange]:
        """The document's lifecycle history, oldest first."""
        result = await self.session.execute(
            select(DocumentStageChange)
            .where(DocumentStageChange.document_id == document_id)
            .order_by(DocumentStageChange.created_at.asc())
        )
        return list(result.scalars().all())

    async def get_draft_comments(self, document_id: uuid.UUID) -> list[DraftComment]:
        """The mirrored draft threads with their replies, oldest first."""
        from sqlalchemy.orm import selectinload

        result = await self.session.execute(
            select(DraftComment)
            .options(selectinload(DraftComment.replies))
            .where(DraftComment.document_id == document_id)
            .order_by(DraftComment.remote_created_at.asc())
        )
        return list(result.scalars().all())

    async def add(self, *rows) -> None:
        """Add rows (signed copies, stage changes) and flush."""
        self.session.add_all(rows)
        await self.session.flush()

    async def is_party(self, user_id: uuid.UUID, document_id: uuid.UUID) -> bool:
        """Check if user is a party on this document (not the owner)"""

        return await self.get_party_role(user_id, document_id) is not None

    async def get_party_role(
        self, user_id: uuid.UUID, document_id: uuid.UUID
    ) -> str | None:
        """Return the user's whitelisted role on this document, or None."""

        result = await self.session.execute(
            select(DocumentParty.role).where(
                DocumentParty.document_id == document_id,
                DocumentParty.user_id == user_id,
                DocumentParty.status == PARTY_STATUS_ACCEPTED,
            )
        )

        return result.scalar_one_or_none()

    async def get_party_rows_for_user(
        self, user_id: uuid.UUID
    ) -> dict[uuid.UUID, DocumentParty]:
        """Every document this user was invited to, keyed by document id.

        One query rather than per-document lookups, so the document list can
        show which entries are shared with the user and when.
        """
        result = await self.session.execute(
            select(DocumentParty).where(
                DocumentParty.user_id == user_id,
                DocumentParty.status == PARTY_STATUS_ACCEPTED,
            )
        )
        return {row.document_id: row for row in result.scalars().all()}

    async def get_pending_invitations(self, user_id: uuid.UUID) -> list:
        """Invitations awaiting this user's response, newest first."""
        from sqlalchemy.orm import selectinload

        result = await self.session.execute(
            select(DocumentParty)
            .options(selectinload(DocumentParty.document))
            .where(
                DocumentParty.user_id == user_id,
                DocumentParty.status == PARTY_STATUS_PENDING,
            )
            .order_by(DocumentParty.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_invitation(self, invitation_id: uuid.UUID, user_id: uuid.UUID):
        """Fetch an invitation, scoped to the user it was addressed to."""
        from sqlalchemy.orm import selectinload

        result = await self.session.execute(
            select(DocumentParty)
            .options(selectinload(DocumentParty.document))
            .where(
                DocumentParty.id == invitation_id,
                DocumentParty.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_party(self, document_id: uuid.UUID, user_id: uuid.UUID):
        """Any party row regardless of status, for re-invite handling."""
        result = await self.session.execute(
            select(DocumentParty).where(
                DocumentParty.document_id == document_id,
                DocumentParty.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_parties(self, document_id) -> list:
        from sqlalchemy.orm import selectinload

        result = await self.session.execute(
            select(DocumentParty)
            .options(selectinload(DocumentParty.user))
            .where(DocumentParty.document_id == document_id)
        )
        return list(result.scalars().all())

    async def add_party(
        self, document_id: uuid.UUID, user_id: uuid.UUID, role: str, email: str
    ):
        """Add a party to a document"""

        party = DocumentParty(
            document_id=document_id, user_id=user_id, role=role, email=email
        )
        self.session.add(party)
        await self.session.flush()
        return party

    async def drop_draft_revocation(self, draft_file_id: str, permission_id: str) -> None:
        """Forget a pending revocation of this permission (its party is back)."""
        await self.session.execute(
            delete(DraftRevocation).where(
                DraftRevocation.draft_file_id == draft_file_id,
                DraftRevocation.permission_id == permission_id,
            )
        )

    async def remove_party(self, document_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Remove a party from a document. Returns True if removed (also get removed in the table)"""

        stmt = select(DocumentParty).where(
            DocumentParty.document_id == document_id, DocumentParty.user_id == user_id
        )

        result = await self.session.execute(stmt)

        party = result.scalar_one_or_none()
        if party:
            await self.session.delete(party)
            await self.session.flush()
            return True

        return False

    async def count_active(self) -> int:
        """Count documents that were not cancelled."""
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count())
            .select_from(self.model)
            .where(self.model.lifecycle != LIFECYCLE_CANCELLED)
        )
        return result.scalar() or 0

    async def count_by_status(self, status: str) -> int:
        """Count documents by processing status."""
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count())
            .select_from(self.model)
            .where(self.model.status == status)
        )
        return result.scalar() or 0
