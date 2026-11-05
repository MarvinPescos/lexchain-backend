from app.infrastructure.database.base_repository import BaseRepository
from sqlalchemy import func, select
from datetime import datetime
import uuid

from app.features.documents.models import Document

from .constants import VERIFY_TAMPER_FINDINGS
from .models import OnChainRecord, VerificationResult

class OnChainRecordRepository(BaseRepository[OnChainRecord]):
    def __init__(self, session):
        super().__init__(OnChainRecord, session)
    
    async def get_by_document_id(self, document_id: uuid.UUID) -> OnChainRecord | None:
        result = await self.session.execute(
            select(OnChainRecord)
            .where(OnChainRecord.document_id == document_id)
        )
        return result.scalar_one_or_none()


class VerificationResultRepository(BaseRepository[VerificationResult]):
    def __init__(self, session):
        super().__init__(VerificationResult, session)

    async def latest_scheduled_finding(
        self, document_id: uuid.UUID
    ) -> VerificationResult | None:
        """The last tamper finding the scheduled check wrote, which it alerted on."""
        result = await self.session.execute(
            select(VerificationResult)
            .where(
                VerificationResult.document_id == document_id,
                VerificationResult.verified_by.is_(None),
                VerificationResult.status.in_(VERIFY_TAMPER_FINDINGS),
            )
            .order_by(VerificationResult.verified_at.desc(), VerificationResult.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _for_owner(query, owner_id, verified_from, verified_to):
        query = query.join(Document, Document.id == VerificationResult.document_id).where(
            Document.user_id == owner_id
        )
        if verified_from:
            query = query.where(VerificationResult.verified_at >= verified_from)
        if verified_to:
            query = query.where(VerificationResult.verified_at < verified_to)
        return query

    async def count_by_status_for_owner(
        self,
        owner_id: uuid.UUID,
        verified_from: datetime | None,
        verified_to: datetime | None,
    ) -> dict[str, int]:
        result = await self.session.execute(
            self._for_owner(
                select(VerificationResult.status, func.count()),
                owner_id,
                verified_from,
                verified_to,
            ).group_by(VerificationResult.status)
        )
        return dict(result.tuples().all())

    async def list_for_owner(
        self,
        owner_id: uuid.UUID,
        verified_from: datetime | None,
        verified_to: datetime | None,
        limit: int,
        offset: int,
    ) -> list[tuple[VerificationResult, str | None]]:
        """Newest first; `id` breaks ties so a page boundary never splits them unevenly."""
        result = await self.session.execute(
            self._for_owner(
                select(VerificationResult, Document.file_name),
                owner_id,
                verified_from,
                verified_to,
            )
            .order_by(VerificationResult.verified_at.desc(), VerificationResult.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.tuples().all())
