import uuid

from app.features.documents.repository import DocumentRepository
from app.infrastructure.blockchain import BlockchainProvider, ChainRecord
from app.infrastructure.observability.logging_setup import log
from app.shared.errors.exceptions import BadRequestError, NotFoundError
from sqlalchemy.ext.asyncio import AsyncSession

from .repository import OnChainRecordRepository
from .schemas import OnChainVerificationResponse, RecordResponse


class BlockchainService:
    def __init__(self, session: AsyncSession, provider: BlockchainProvider) -> None:
        self.session = session
        self.provider = provider
        self.onchain_repo = OnChainRecordRepository(session)
        self.doc_repo = DocumentRepository(session)

    async def record_document(
        self,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
    ) -> RecordResponse:
        """ """
        document = await self.doc_repo.get_one(user_id, document_id)
        if not document:
            raise NotFoundError("Document not found")

        if document.status != "COMPLETED":
            raise BadRequestError("Document must be fully processed before recording")

        existing = await self.onchain_repo.get_by_document_id(document_id)

        if existing:
            raise BadRequestError("Document already recorded on-chain")

        record: ChainRecord = await self.provider.store_document_hash(
            document_id=str(document_id),
            data_hash=document.document_hash,
        )

        onchain = await self.onchain_repo.create(
            document_id=document_id,
            tx_hash=record.tx_hash,
            onchain_document_id=record.document_id,
            data_hash=record.data_hash,
            onchain_timestamp=record.timestamp,
            issued_by=record.issued_by,
        )

        doc_on_chain = await self.doc_repo.update(document, on_chain=True)

        await self.session.commit()

        log.info(
            "blockchain.record",
            user_id=str(user_id),
            document_id=str(document_id),
            tx_hash=record.tx_hash,
        )

        from app.celery_tasks import handle_document_recorded

        handle_document_recorded.delay(
            user_id=str(user_id),
            document_title=document.file_name,
            document_id=str(document_id),
            tx_hash=onchain.tx_hash,
        )

        return RecordResponse(
            document_id=document.id,
            tx_hash=onchain.tx_hash,
            onchain_document_id=onchain.onchain_document_id,
            data_hash=onchain.data_hash,
        )

    async def verify_document(
        self, user_id: uuid.UUID, document_id: uuid.UUID
    ) -> OnChainVerificationResponse:
        """"""
        document = await self.doc_repo.get_one(user_id, document_id)
        if not document:
            is_party = await self.doc_repo.is_party(user_id, document_id)
            if not is_party:
                raise NotFoundError("Document not found")

        record: ChainRecord | None = await self.provider.get_document_hash(
            document_id=str(document_id)
        )

        if record is None:
            raise NotFoundError("No on-chain record found for this document")

        from datetime import datetime, timezone

        return OnChainVerificationResponse(
            document_id=document.id,
            onchain_document_id=record.document_id,
            data_hash=record.data_hash,
            onchain_timestamp=record.timestamp,
            issued_by=record.issued_by,
            verified_at=datetime.now(timezone.utc),
        )
