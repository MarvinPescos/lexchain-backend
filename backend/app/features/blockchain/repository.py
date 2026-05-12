from app.infrastructure.database.base_repository import BaseRepository
from sqlalchemy import select
import uuid

from .models import OnChainRecord

class OnChainRecordRepository(BaseRepository[OnChainRecord]):
    def __init__(self, session):
        super().__init__(OnChainRecord, session)
    
    async def get_by_document_id(self, document_id: uuid.UUID) -> OnChainRecord | None:
        result = await self.session.execute(
            select(OnChainRecord)
            .where(OnChainRecord.document_id == document_id)
        )
        return result.scalar_one_or_none()
    