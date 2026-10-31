import hashlib
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx
from storage3.utils import StorageException

from app.features.documents.constants import LIFECYCLE_FINALIZED
from app.features.documents.extraction.blocks import blocks_from_text
from app.features.documents.models import Document
from app.features.documents.repository import DocumentRepository
from app.features.snapshot.models import DocumentSnapshot
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.infrastructure.backup import BackupProvider
from app.infrastructure.blockchain import BlockchainProvider, ChainRecord
from app.infrastructure.nlp.ocr import OCRProvider, build_ocr
from app.infrastructure.observability.logging_setup import log
from app.shared.errors.exceptions import NotFoundError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from .constants import (
    VERIFY_AUTHENTIC,
    VERIFY_NOT_ANCHORED,
    VERIFY_SNAPSHOT_COMPROMISED,
    VERIFY_TAMPERED,
    VERIFY_UNAVAILABLE,
)
from .models import VerificationResult
from .repository import OnChainRecordRepository
from .schemas import (
    DocumentVerificationResponse,
    RecordResponse,
    TamperReport,
)
from .tamper import diff_blocks

if TYPE_CHECKING:
    from app.infrastructure.security.dependencies import AuthenticatedUser


def _normalise_hash(value: str) -> str:
    """Chain reads come back with or without the 0x prefix depending on the provider."""
    return value.lower().removeprefix("0x")


class BlockchainService:
    def __init__(
        self,
        session: AsyncSession,
        provider: BlockchainProvider,
        backup: BackupProvider | None = None,
        ocr_for: Callable[[bytes], OCRProvider] = build_ocr,
    ) -> None:
        self.session = session
        self.provider = provider
        # The archive is the only diff baseline; without it a TAMPERED verdict
        # comes with no tamper report.
        self.backup = backup
        self.ocr_for = ocr_for
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
        self,
        user: "AuthenticatedUser",
        document_id: uuid.UUID,
        *,
        tamper_report: bool = True,
        record: bool = False,
    ) -> DocumentVerificationResponse:
        """Verify, and with `record` keep the verdict in the verification history.

        Only a check that reaches a verdict is kept; one that raises is not.
        Internal checks (restore) leave `record` off: they are not report rows.
        """
        result = await self._verify(user, document_id, tamper_report=tamper_report)
        if record:
            await self._record(user, result)
        return result

    async def _record(
        self, user: "AuthenticatedUser", result: DocumentVerificationResponse
    ) -> None:
        """Best-effort: the verdict is already decided, and a failed history
        write must not turn it into an error for the caller."""
        self.session.add(
            VerificationResult(
                document_id=result.document_id,
                verified_by=user.user_id,
                verified_at=result.verified_at,
                status=result.status,
                is_authentic=result.is_authentic,
                baseline_trusted=result.baseline_trusted,
                onchain_hash=result.onchain_hash,
                snapshot_hash=result.snapshot_hash,
                current_hash=result.current_hash,
                tx_hash=result.tx_hash or None,
                tamper_report_status=result.tamper_report_status,
            )
        )
        try:
            await self.session.commit()
        except SQLAlchemyError as e:
            await self.session.rollback()
            log.error(
                "blockchain.verify.record_failed",
                document_id=str(result.document_id),
                status=result.status,
                error=f"{type(e).__name__}: {e}",
            )

    async def _verify(
        self,
        user: "AuthenticatedUser",
        document_id: uuid.UUID,
        *,
        tamper_report: bool,
    ) -> DocumentVerificationResponse:
        """Verify a finalized document against its on-chain anchor.

        Hashes the stored signed copy as it is right now and compares it with
        the chain. The judgment is made here, server-side; a client is never
        trusted to decide whether something is authentic. The snapshot hash is
        checked too, so an edit to the frozen record in the database is reported.
        `tamper_report=False` skips the diff for callers that need only the verdict.
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
                status=VERIFY_NOT_ANCHORED,
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
                status=VERIFY_UNAVAILABLE,
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
                status=VERIFY_AUTHENTIC,
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
                status=VERIFY_SNAPSHOT_COMPROMISED,
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
            status=VERIFY_TAMPERED,
            is_authentic=False,
            baseline_trusted=True,
            current_hash=current_hash,
            message="The stored file does NOT match the hash anchored on-chain.",
            **common,
            **chain,
            **(
                await self._tamper_report(snapshot, onchain_hash, current)
                if tamper_report
                else {}
            ),
        )

    async def _tamper_report(
        self, snapshot: DocumentSnapshot, onchain_hash: str, current: bytes
    ) -> dict[str, Any]:
        """Diff the archived original against the current file, both extracted fresh.

        Best-effort: the TAMPERED verdict is already decided by the hashes, so
        any failure here only marks the report UNAVAILABLE. The stored
        extraction is never used — it may not describe either file's bytes.
        """
        document_id = str(snapshot.document_id)

        def unavailable(reason: str) -> dict[str, Any]:
            log.warning(
                "blockchain.verify.tamper_report_unavailable",
                document_id=document_id,
                reason=reason,
            )
            return {
                "tamper_report_status": "UNAVAILABLE",
                "tamper_report_unavailable_reason": reason,
            }

        if self.backup is None or snapshot.backup_key is None:
            return unavailable("No archived original to compare against.")
        try:
            original = await self.backup.fetch(snapshot.backup_key)
        except Exception as e:
            log.error(
                "blockchain.verify.archive_fetch_failed",
                document_id=document_id,
                error=f"{type(e).__name__}: {e}",
            )
            return unavailable("The archived original could not be read.")
        if hashlib.sha256(original).hexdigest() != onchain_hash:
            log.error("blockchain.verify.archive_mismatch", document_id=document_id)
            return unavailable("The archived original does not match the chain.")

        try:
            # One engine for both sides: `build_ocr` picks by content, and two
            # engines split text into blocks differently, which would read as
            # changes that are not there.
            ocr = self.ocr_for(original)
            original_result = await ocr.extract(original)
            current_result = await ocr.extract(current)
            diff = diff_blocks(
                original_result.blocks or blocks_from_text(original_result.ingestion_text),
                current_result.blocks or blocks_from_text(current_result.ingestion_text),
            )
            report = TamperReport.model_validate(
                {**diff, "localized": bool(original_result.blocks and current_result.blocks)}
            )
        except Exception as e:
            log.error(
                "blockchain.verify.tamper_diff_failed",
                document_id=document_id,
                error=f"{type(e).__name__}: {e}",
            )
            return unavailable("The text of the documents could not be compared.")

        if not report.segments:
            return unavailable(
                "The file changed, but its extracted text did not; the change is "
                "outside the text and cannot be located."
            )
        return {"tamper_report_status": "AVAILABLE", "tamper_report": report}
