import hashlib
import uuid
from typing import TYPE_CHECKING

import httpx
from storage3.utils import StorageException

from app.features.documents.constants import LIFECYCLE_FINALIZED
from app.features.documents.models import Document
from app.features.documents.repository import DocumentRepository
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.infrastructure.blockchain import BlockchainProvider, ChainRecord
from app.infrastructure.observability.logging_setup import log
from app.shared.errors.exceptions import NotFoundError
from sqlalchemy.ext.asyncio import AsyncSession

from .repository import OnChainRecordRepository
from .schemas import (
    DocumentVerificationResponse,
    RecordResponse,
)

if TYPE_CHECKING:
    from app.infrastructure.security.dependencies import AuthenticatedUser


def _normalise_hash(value: str) -> str:
    """Chain reads come back with or without the 0x prefix depending on the provider."""
    return value.lower().removeprefix("0x")


class BlockchainService:
    def __init__(self, session: AsyncSession, provider: BlockchainProvider) -> None:
        self.session = session
        self.provider = provider
        self.onchain_repo = OnChainRecordRepository(session)
        self.doc_repo = DocumentRepository(session)
        self.snapshot_repo = DocumentSnaphotRepository(session)

    async def record_document(
        self,
        document: Document,
        data_hash: str
    ) -> RecordResponse:
        """Anchor a document's hash on-chain.

        Internal step of DocumentFinalization.finalize, which has already
        validated the document and derived data_hash from the snapshot.
        """

        record: ChainRecord = await self.provider.store_document_hash(
            document_id=str(document.id),
            data_hash=data_hash,
        )

        onchain = await self.onchain_repo.create(
            document_id=document.id,
            tx_hash=record.tx_hash,
            onchain_document_id=record.document_id,
            data_hash=record.data_hash,
            onchain_timestamp=record.timestamp,
            issued_by=record.issued_by,
        )

        await self.doc_repo.update(document, on_chain=True)


        log.info(
            "blockchain.record",
            document_id=str(document.id),
            tx_hash=record.tx_hash,
        )

        return RecordResponse(
            document_id=document.id,
            tx_hash=onchain.tx_hash,
            onchain_document_id=onchain.onchain_document_id,
            data_hash=onchain.data_hash,
        )

    async def verify_document(
        self, user: "AuthenticatedUser", document_id: uuid.UUID
    ) -> DocumentVerificationResponse:
        """Verify a finalized document against its on-chain anchor.

        Hashes the stored signed copy as it is right now and compares it with
        the chain. The judgment is made here, server-side; a client is never
        trusted to decide whether something is authentic. The snapshot hash is
        checked too, so an edit to the frozen record in the database is reported.
        """
        from datetime import datetime, timezone

        from app.features.documents.access import DocumentAccess
        from app.infrastructure.clients.supabase_storage import get_storage_client

        document = await DocumentAccess(self.session).authorize(user, document_id)
        await self.session.refresh(document, ["signed_copy"])
        copy = document.signed_copy

        common = dict(
            document_id=document_id,
            file_name=document.file_name,
            finalized_at=document.finalized_at,
            verified_at=datetime.now(timezone.utc),
        )

        if document.lifecycle != LIFECYCLE_FINALIZED:
            return DocumentVerificationResponse(
                status="NOT_ANCHORED",
                is_authentic=False,
                baseline_trusted=False,
                message=(
                    "This document has not been finalized, so nothing was anchored "
                    "on-chain and there is nothing to verify against."
                ),
                **common,
            )

        record: ChainRecord | None = await self.provider.get_document_hash(
            document_id=str(document_id)
        )
        if record is None:
            raise NotFoundError("No on-chain record found for this document")
        onchain_hash = _normalise_hash(record.data_hash)

        snapshot = await self.snapshot_repo.get_by_document_id(document_id)
        if not snapshot:
            raise NotFoundError("No stored original found for this document")
        baseline_trusted = snapshot.file_sha256 == onchain_hash

        chain = dict(
            onchain_hash=onchain_hash,
            snapshot_hash=snapshot.file_sha256,
            tx_hash=record.tx_hash,
            onchain_timestamp=record.timestamp,
            issued_by=record.issued_by,
        )

        try:
            # Never through the CDN: a cached copy would be compared against the
            # chain and produce a confident, wrong verdict.
            current = await get_storage_client().download(copy.storage_url)
        except (httpx.HTTPError, OSError, StorageException) as e:
            log.error(
                "blockchain.verify.unavailable",
                document_id=str(document_id),
                error=f"{type(e).__name__}: {e}",
            )
            return DocumentVerificationResponse(
                status="VERIFICATION_UNAVAILABLE",
                is_authentic=False,
                baseline_trusted=baseline_trusted,
                message=(
                    "Could not read the stored file to verify it. This is NOT a "
                    "tamper result; the document's integrity is unknown. Try again "
                    "once storage is available."
                ),
                **common,
                **chain,
            )

        current_hash = hashlib.sha256(current).hexdigest()

        if current_hash == onchain_hash:
            log.info("blockchain.verify.authentic", document_id=str(document_id))
            return DocumentVerificationResponse(
                status="AUTHENTIC",
                is_authentic=True,
                baseline_trusted=baseline_trusted,
                current_hash=current_hash,
                message="The stored file matches the hash anchored on-chain.",
                **common,
                **chain,
            )

        if not baseline_trusted:
            log.error(
                "blockchain.verify.snapshot_compromised",
                document_id=str(document_id),
                onchain_hash=onchain_hash,
                snapshot_hash=snapshot.file_sha256,
            )
            return DocumentVerificationResponse(
                status="SNAPSHOT_COMPROMISED",
                is_authentic=False,
                baseline_trusted=False,
                current_hash=current_hash,
                message=(
                    "The stored file does not match the chain, and the frozen "
                    "record in the database was altered as well."
                ),
                **common,
                **chain,
            )

        log.warning("blockchain.verify.tampered", document_id=str(document_id))
        return DocumentVerificationResponse(
            status="TAMPERED",
            is_authentic=False,
            baseline_trusted=True,
            current_hash=current_hash,
            message="The stored file does NOT match the hash anchored on-chain.",
            **common,
            **chain,
        )
