import uuid

from datetime import datetime, timedelta, timezone
from typing import cast

from sqlalchemy import CursorResult, delete, func, or_, select, update
from sqlalchemy.orm import selectinload

from app.features.books.models import Book
from app.infrastructure.database import UserScopeRepository
from app.shared.constants import (
    PARTY_STATUS_ACCEPTED,
    PARTY_STATUS_DECLINED,
    PARTY_STATUS_PENDING,
)

from .constants import (
    DELIVERY_FAILED,
    DELIVERY_SENDING,
    DELIVERY_STALE_AFTER_SECONDS,
    ECOPY_APPROVED,
    ECOPY_PENDING,
    LIFECYCLE_CANCELLED,
    LIFECYCLE_READY_FOR_SIGNATURE,
)
from .schemas import RegisterReference
from .models import (
    Document,
    DocumentParty,
    DocumentStageChange,
    DraftComment,
    DraftRevocation,
    EcopyRequest,
    SignedCopy,
)


def _readable_by(user_id: uuid.UUID):
    """Documents the user owns or is an accepted party of."""
    # A pending invitation is not access — it must be accepted first.
    shared_subq = select(DocumentParty.document_id).where(
        DocumentParty.user_id == user_id,
        DocumentParty.status == PARTY_STATUS_ACCEPTED,
    )
    return or_(Document.user_id == user_id, Document.id.in_(shared_subq))


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
        stmt = (
            select(Document)
            .where(
                _readable_by(user_id),
                *([Document.lifecycle == lifecycle] if lifecycle else []),
            )
            .order_by(Document.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        result = await self.session.execute(stmt)

        return list(result.scalars().all())

    async def find_by_register_reference(
        self, user_id: uuid.UUID, reference: RegisterReference
    ) -> list[Document]:
        """Documents the user may read filed under exactly this register entry.

        Book numbers are only unique per lawyer, so more than one can match.
        """
        result = await self.session.execute(
            select(Document)
            .join(Book, Book.id == Document.book_id)
            .where(
                _readable_by(user_id),
                Document.doc_no == reference.doc_no,
                Document.page_no == reference.page_no,
                Book.book_number == reference.book_number,
                Book.series_year == reference.series_year,
            )
            .order_by(Document.created_at.asc())
        )
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

    async def signed_copies_with_hash(
        self, document_id: uuid.UUID, sha256: str
    ) -> list[SignedCopy]:
        """Oldest first: a restore adds later copies of the same bytes."""
        result = await self.session.execute(
            select(SignedCopy)
            .where(SignedCopy.document_id == document_id, SignedCopy.sha256 == sha256)
            .order_by(SignedCopy.created_at)
        )
        return list(result.scalars().all())

    async def get_signed_copy(
        self, document_id: uuid.UUID, copy_id: uuid.UUID
    ) -> SignedCopy | None:
        result = await self.session.execute(
            select(SignedCopy).where(
                SignedCopy.document_id == document_id, SignedCopy.id == copy_id
            )
        )
        return result.scalar_one_or_none()

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

    async def get_email_invite(
        self, document_id: uuid.UUID, email: str
    ) -> DocumentParty | None:
        """The open invitation to an address that has no account yet."""
        result = await self.session.execute(
            select(DocumentParty).where(
                DocumentParty.document_id == document_id,
                DocumentParty.user_id.is_(None),
                func.lower(DocumentParty.email) == email.lower(),
            )
        )
        return result.scalar_one_or_none()

    async def get_email_invite_by_id(
        self, document_id: uuid.UUID, party_id: uuid.UUID
    ) -> DocumentParty | None:
        result = await self.session.execute(
            select(DocumentParty).where(
                DocumentParty.id == party_id,
                DocumentParty.document_id == document_id,
                DocumentParty.user_id.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def claim_email_invites(
        self, user_id: uuid.UUID, email: str
    ) -> list[DocumentParty]:
        """Hand every unexpired email invitation for this address to its account.

        Row-locked, so two sign-ins at once cannot both claim the same row. A
        document the account was meanwhile added to directly keeps that party
        row and drops the invitation, so the claim never collides with it.
        """
        now = datetime.now(timezone.utc)
        result = await self.session.execute(
            select(DocumentParty)
            .where(
                DocumentParty.user_id.is_(None),
                func.lower(DocumentParty.email) == email.lower(),
                DocumentParty.invite_expires_at > now,
            )
            .with_for_update(skip_locked=True)
        )
        claimed = []
        for party in result.scalars().all():
            if await self.get_party(party.document_id, user_id) is not None:
                await self.session.delete(party)
                continue
            party.user_id = user_id
            party.status = PARTY_STATUS_ACCEPTED
            party.responded_at = now
            claimed.append(party)
        await self.session.flush()
        return claimed

    async def add_party(
        self, document_id: uuid.UUID, user_id: uuid.UUID | None, role: str, email: str
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

    # === E-copy requests ===

    async def get_ecopy_request(
        self,
        document_id: uuid.UUID,
        request_id: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> EcopyRequest | None:
        """One request on this document. `for_update` locks the row, so two
        decisions on the same request run one after the other."""
        stmt = select(EcopyRequest).where(
            EcopyRequest.id == request_id, EcopyRequest.document_id == document_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_pending_ecopy(
        self, document_id: uuid.UUID, client_id: uuid.UUID
    ) -> EcopyRequest | None:
        result = await self.session.execute(
            select(EcopyRequest).where(
                EcopyRequest.document_id == document_id,
                EcopyRequest.client_id == client_id,
                EcopyRequest.status == ECOPY_PENDING,
            )
        )
        return result.scalar_one_or_none()

    async def has_approved_ecopy(
        self, document_id: uuid.UUID, client_id: uuid.UUID, sha256: str
    ) -> bool:
        """Whether this client was approved a copy of exactly these bytes."""
        result = await self.session.execute(
            select(EcopyRequest.id)
            .where(
                EcopyRequest.document_id == document_id,
                EcopyRequest.client_id == client_id,
                EcopyRequest.status == ECOPY_APPROVED,
                EcopyRequest.approved_sha256 == sha256,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def list_ecopy_requests(
        self,
        *,
        document_id: uuid.UUID | None = None,
        client_id: uuid.UUID | None = None,
        owner_id: uuid.UUID | None = None,
        status: str | None = None,
    ) -> list[EcopyRequest]:
        """Requests matching every filter given, newest first."""
        stmt = select(EcopyRequest).options(selectinload(EcopyRequest.events))
        if document_id is not None:
            stmt = stmt.where(EcopyRequest.document_id == document_id)
        if client_id is not None:
            stmt = stmt.where(EcopyRequest.client_id == client_id)
        if owner_id is not None:
            stmt = stmt.join(Document, Document.id == EcopyRequest.document_id).where(
                Document.user_id == owner_id
            )
        if status is not None:
            stmt = stmt.where(EcopyRequest.status == status)
        result = await self.session.execute(
            stmt.order_by(EcopyRequest.created_at.desc())
        )
        return list(result.scalars().all())

    async def claim_ecopy_delivery(self, request_id: uuid.UUID) -> bool:
        """Mark an approved request SENDING if it is owed a send; True if this
        caller won it.

        One conditional UPDATE, so of two concurrent retries only one sends.
        A send stuck in SENDING (its worker died) can be claimed again once stale.
        """
        now = datetime.now(timezone.utc)
        stale = now - timedelta(seconds=DELIVERY_STALE_AFTER_SECONDS)
        result = await self.session.execute(
            update(EcopyRequest)
            .where(
                EcopyRequest.id == request_id,
                EcopyRequest.status == ECOPY_APPROVED,
                or_(
                    EcopyRequest.delivery_status == DELIVERY_FAILED,
                    (EcopyRequest.delivery_status == DELIVERY_SENDING)
                    & (EcopyRequest.delivery_started_at < stale),
                ),
            )
            .values(
                delivery_status=DELIVERY_SENDING,
                delivery_started_at=now,
                delivery_attempts=EcopyRequest.delivery_attempts + 1,
                delivery_error=None,
            )
            .execution_options(synchronize_session=False)
        )
        return cast(CursorResult, result).rowcount == 1

    async def lock_signed_copy_id(self, document_id: uuid.UUID) -> uuid.UUID | None:
        """The document's current signed copy, with its row locked until commit."""
        return await self.session.scalar(
            select(Document.signed_copy_id)
            .where(Document.id == document_id)
            .with_for_update()
        )

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
