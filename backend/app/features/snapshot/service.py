import uuid

from app.infrastructure.observability.logging_setup import log
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.errors.exceptions import NotFoundError

from .repository import DocumentSnaphotRepository
from .schemas import DocumentSnapshotResponse


class DocumentSnapshotService:
    """Service layer for Snapshot management"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = DocumentSnaphotRepository(session)

    async def get_snapshot(self, snapshot_id: uuid.UUID) -> DocumentSnapshotResponse:
        snapshot = await self.repo.get_by_id(snapshot_id)
        if not snapshot:
            raise NotFoundError("Snapshot not found")
        log.info("snapshot.fetch.one", snapshot_id=str(snapshot_id))
        return DocumentSnapshotResponse.model_validate(snapshot)

    async def create_snapshot(
        self,
        document_id: uuid.UUID,
        signed_copy_id: uuid.UUID,
        file_sha256: str,
        size_bytes: int,
    ) -> DocumentSnapshotResponse:
        """Freeze the signed copy being finalized (once, at finalize)."""
        snapshot = await self.repo.create(
            document_id=document_id,
            signed_copy_id=signed_copy_id,
            file_sha256=file_sha256,
            size_bytes=size_bytes,
        )
        # created_at is a server-side default; load it before serialising.
        await self.session.refresh(snapshot)

        log.info(
            "snapshot.create", document_id=str(document_id), file_sha256=file_sha256
        )
        return DocumentSnapshotResponse.model_validate(snapshot)
