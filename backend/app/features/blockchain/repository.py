from app.infrastructure.database.base_repository import BaseRepository
from sqlalchemy import ColumnElement, func, or_, select
from datetime import datetime
import uuid

from app.features.documents.constants import ACTION_RESTORE
from app.features.documents.models import Document, DocumentStageChange

from .constants import VERIFY_RECORD_ALTERED, VERIFY_TAMPER_FINDINGS
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

    @staticmethod
    def _since_last_restore() -> ColumnElement[bool]:
        last_restore = (
            select(func.max(DocumentStageChange.created_at))
            .where(
                DocumentStageChange.document_id == VerificationResult.document_id,
                DocumentStageChange.action == ACTION_RESTORE,
            )
            .scalar_subquery()
        )
        return or_(last_restore.is_(None), VerificationResult.verified_at > last_restore)

    async def last_restores(self) -> dict[str, datetime]:
        """When each restored document was last restored, by document ID."""
        result = await self.session.execute(
            select(DocumentStageChange.document_id, func.max(DocumentStageChange.created_at))
            .where(DocumentStageChange.action == ACTION_RESTORE)
            .group_by(DocumentStageChange.document_id)
        )
        return {str(document_id): at for document_id, at in result.tuples().all()}

    async def latest_scheduled_finding(
        self, document_id: uuid.UUID
    ) -> VerificationResult | None:
        """The last tamper finding the scheduled check wrote, which it alerted on.

        One from before a restore doesn't count: that problem was fixed, so the
        same file turning up again is a new tampering.
        """
        result = await self.session.execute(
            select(VerificationResult)
            .where(
                VerificationResult.document_id == document_id,
                VerificationResult.verified_by.is_(None),
                VerificationResult.status.in_(VERIFY_TAMPER_FINDINGS),
                self._since_last_restore(),
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

    async def open_findings_for_owner(
        self, owner_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, datetime]]:
        """Documents with a finding since their last restore, and when it was
        first seen, newest first."""
        first_seen = func.min(VerificationResult.verified_at)
        result = await self.session.execute(
            select(VerificationResult.document_id, first_seen)
            .join(Document, Document.id == VerificationResult.document_id)
            .where(
                Document.user_id == owner_id,
                VerificationResult.status.in_(
                    (*VERIFY_TAMPER_FINDINGS, VERIFY_RECORD_ALTERED)
                ),
                self._since_last_restore(),
            )
            .group_by(VerificationResult.document_id)
            .order_by(first_seen.desc())
        )
        return list(result.tuples().all())

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
