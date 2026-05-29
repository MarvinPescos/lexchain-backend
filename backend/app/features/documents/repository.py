import uuid

from app.infrastructure.database import UserScopeRepository
from sqlalchemy import or_, select

from .models import Document, DocumentParty


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
    ) -> list[Document]:
        """Return document the user owns OR is a party of"""
        shared_subq = select(DocumentParty.document_id).where(
            DocumentParty.user_id == user_id
        )

        stmt = (
            select(Document)
            .where(
                or_(Document.user_id == user_id, Document.id.in_(shared_subq)),
                Document.is_latest,
            )
            .order_by(Document.created_at.desc())
            .limit(limit)
            .offset(offset)
        )

        result = await self.session.execute(stmt)

        return list(result.scalars().all())

    async def supersede(self, document: Document) -> None:
        """Mark a document version as no longer the latest"""
        await self.update(document, is_latest=False)

    async def get_version_history(
        self, document_id: uuid.UUID
    ) -> list[Document] | None:
        """
        Walk the linked list backward to build the full version chain.
        Returns [newest, ..., oldest].
        """
        from sqlalchemy.orm import selectinload

        stmt = (
            select(Document)
            .options(selectinload(Document.on_chain_record))
            .where(Document.id == document_id)
        )
        result = await self.session.execute(stmt)
        current = result.scalar_one_or_none()

        # empty
        if not current:
            return

            # Step 1: Walk forward to find the absolute latest version
        while True:
            next_stmt = (
                select(Document)
                .options(selectinload(Document.on_chain_record))
                .where(Document.previous_document_id == current.id)
                .order_by(Document.created_at.desc())
            )
            next_result = await self.session.execute(next_stmt)
            next_doc = next_result.scalars().first()
            if next_doc:
                current = next_doc
            else:
                break
        # Step 2: Walk backward from latest to oldest
        history = []
        while current:
            history.append(current)
            if current.previous_document_id:
                prev_stmt = (
                    select(Document)
                    .options(selectinload(Document.on_chain_record))
                    .where(Document.id == current.previous_document_id)
                )
                prev_result = await self.session.execute(prev_stmt)
                current = prev_result.scalar_one_or_none()
            else:
                break
        return history

    # async def get_latest_of_chain(self, document_id: uuid.UUID) -> Document  | None:

    async def is_party(self, user_id: uuid.UUID, document_id: uuid.UUID) -> bool:
        """Check if user is a party on this document (not the owner)"""

        result = await self.session.execute(
            select(DocumentParty).where(
                DocumentParty.document_id == document_id,
                DocumentParty.user_id == user_id,
            )
        )

        return result.scalar_one_or_none() is not None

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

    async def get_by_content_hash(self, content_hash: str) -> Document | None:
        from app.features.blockchain.models import OnChainRecord

        result = await self.session.execute(
            select(self.model)
            .outerjoin(OnChainRecord, self.model.id == OnChainRecord.document_id)
            .where(
                self.model.content_hash == content_hash,
                self.model.content_hash.is_not(None),
            )
            .order_by(
                OnChainRecord.id.is_not(None).desc(), self.model.created_at.desc()
            )
        )
        return result.scalars().first()

    async def count_latest(self) -> int:
        """Count only latest document versions."""
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count()).select_from(self.model).where(self.model.is_latest)
        )
        return result.scalar() or 0

    async def count_by_status(self, status: str) -> int:
        """Count latest documents by processing status."""
        from sqlalchemy import func

        result = await self.session.execute(
            select(func.count())
            .select_from(self.model)
            .where(
                self.model.is_latest,
                self.model.status == status,
            )
        )
        return result.scalar() or 0
