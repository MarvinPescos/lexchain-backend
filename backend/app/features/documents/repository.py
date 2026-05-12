from app.infrastructure.database import UserScopeRepository
from sqlalchemy import select, or_
import uuid

from app.features.documents.schemas import DocumentPartyListResponse

from .models import Document, DocumentParty

class DocumentRepository(UserScopeRepository[Document]):
    """Repository pattern for Document"""

    def __init__(self , session):
        super().__init__(Document, session)

    # === Custom methods === 

    async def get_document_for_user(
        self, 
        user_id: uuid.UUID,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Document]:
        """Return document the user owns OR is a party of"""

        owned = select(Document).where(Document.user_id == user_id)

        shared_subq = (
            select(DocumentParty.document_id)
            .where(DocumentParty.user_id == user_id)
        )

        stmt =  (
            select(Document)
            .where(
                or_(
                    Document.user_id == user_id,
                    Document.id.in_(shared_subq)
                )
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

    async def get_version_history(self, document_id: uuid.UUID) -> list[Document]:
        """
            Walk the linked list backward to build the full version chain.  
            Returns [newest, ..., oldest].
        """
        from sqlalchemy.orm import selectinload
        history = []
        
        stmt = select(Document).options(selectinload(Document.on_chain_record)).where(Document.id == document_id)
        result = await self.session.execute(stmt)
        current = result.scalar_one_or_none()

        # empty
        if not current:
            return history

        while current:
            history.append(current)
            if current.previous_document_id:
                stmt = select(Document).options(selectinload(Document.on_chain_record)).where(Document.id == current.previous_document_id)
                result = await self.session.execute(stmt)
                current = result.scalar_one_or_none()
            else:
                break
        
        return history

    # async def get_latest_of_chain(self, document_id: uuid.UUID) -> Document  | None:
        

    async def is_party(self, user_id: uuid.UUID,  document_id: uuid.UUID) -> bool:
        """Check if user is a party on this document (not the owner)"""

        result = await self.session.execute(
            select(DocumentParty).where(
                DocumentParty.document_id == document_id, 
                DocumentParty.user_id == user_id
            )
        )

        return result.scalar_one_or_none() is not None

    async def get_parties(self, document_id) -> list:
        """Get all parties for a document"""
        result = await self.session.execute(
            select(DocumentParty)
            .where(DocumentParty.document_id == document_id)
        )
        return list(result.scalars().all())
    
    async def add_party(self, document_id: uuid.UUID, user_id: uuid.UUID, role: str):
        """Add a party to a document"""

        party = DocumentParty(
            document_id=document_id,
            user_id=user_id,
            role=role
        )
        self.session.add(party)
        await self.session.flush()
        return party
    
    async def remove_party(self, document_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Remove a party from a document. Returns True if removed (also get removed in the table)"""

        stmt = select(DocumentParty).where(
            DocumentParty.document_id == document_id,
            DocumentParty.user_id == user_id
        )

        result = await self.session.execute(stmt)

        party = result.scalar_one_or_none()
        if party:
            await self.session.delete(party)
            await self.session.flush()
            return True

        return False
        
        


