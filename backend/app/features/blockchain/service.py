import asyncio
import hashlib
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx
from storage3.utils import StorageException

from app.core import settings
from app.features.documents.constants import LIFECYCLE_FINALIZED
from app.features.documents.extraction.blocks import blocks_from_text
from app.features.documents.models import Document
from app.features.documents.repository import DocumentRepository
from app.features.snapshot.models import DocumentSnapshot
from app.features.snapshot.records import (
    document_hash_change,
    RecordCheck,
    analysis_anchor_id,
    metadata_anchor_id,
    normalise_hash,
)
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.infrastructure.backup import BackupProvider
from app.infrastructure.blockchain import BlockchainProvider, ChainRecord
from app.infrastructure.nlp.ocr import ExtractionResult, OCRProvider, build_ocr
from app.infrastructure.observability.logging_setup import log
from app.shared.errors.exceptions import NotFoundError, ServiceUnavailableError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from .constants import (
    VERIFY_AUTHENTIC,
    VERIFY_FILE_MISSING,
    VERIFY_NOT_ANCHORED,
    VERIFY_SNAPSHOT_COMPROMISED,
    VERIFY_TAMPER_FINDINGS,
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


# Verify runs on open, so it won't wait out a cold event scan. See
# `corrected_document_ids`: the scan keeps its progress for the next call.
_EVENT_READ_TIMEOUT_SECONDS = 5


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
        self.records = RecordCheck(session, provider, backup)

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
        A quick check (`tamper_report=False`) keeps neither AUTHENTIC nor
        NOT_ANCHORED, so opening documents doesn't flood the integrity report.
        """
        result = await self._verify(user, document_id, tamper_report=tamper_report)
        routine = result.status in (VERIFY_AUTHENTIC, VERIFY_NOT_ANCHORED)
        if record and (tamper_report or not routine):
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
        """The file's verdict, plus the state of the records around it.

        `status` stays about the PDF: a record finding never changes it.
        """
        result = await self._verify_file(user, document_id, tamper_report=tamper_report)
        # The quick check on open reads no archive, so it can't judge `size_bytes`.
        if result.status == VERIFY_NOT_ANCHORED:
            return result

        document = await self.session.get(Document, document_id)
        assert document is not None  # just authorized
        metadata = await self.records.metadata(document)
        analysis = await self.records.analysis(document)
        corrected = await self._anchor_corrected(document_id)
        message = result.message
        if corrected:
            message += (
                " The on-chain anchor of this document was replaced with the "
                "contract owner key, so it isn't the hash from finalize."
            )
        return result.model_copy(
            update={
                "metadata_status": metadata.status,
                "metadata_changes": metadata.changes,
                "analysis_status": analysis.status,
                "analysis_changes": analysis.changes,
                "anchor_corrected": corrected,
                "needs_restore": await self._needs_restore(
                    document,
                    result,
                    "ALTERED" in (metadata.status, analysis.status),
                    tamper_report,
                ),
                "message": message,
            }
        )

    async def _needs_restore(
        self,
        document: Document,
        result: DocumentVerificationResponse,
        record_altered: bool,
        read_archive: bool,
    ) -> bool:
        if result.status in VERIFY_TAMPER_FINDINGS or record_altered:
            return True
        snapshot = await self.snapshot_repo.get_by_document_id(document.id)
        if result.onchain_hash is None or snapshot is None:
            return False
        if document.lifecycle != LIFECYCLE_FINALIZED:
            return True
        if document_hash_change(document, result.onchain_hash) is not None:
            return True
        archived = (
            await self.records.archived_pdf(document.id, result.onchain_hash)
            if read_archive
            else None
        )
        return bool(
            await self.records.snapshot_changes(snapshot, result.onchain_hash, archived)
        )

    async def _anchor_corrected(self, document_id: uuid.UUID) -> bool | None:
        """Whether any of the document's anchors was replaced. None if unknown."""
        if settings.CHAIN_SCAN_FROM_BLOCK is None:
            return None
        try:
            corrected = await asyncio.wait_for(
                self.provider.corrected_document_ids(settings.CHAIN_SCAN_FROM_BLOCK),
                _EVENT_READ_TIMEOUT_SECONDS,
            )
        except (ServiceUnavailableError, TimeoutError) as e:
            log.warning(
                "blockchain.verify.events_unavailable",
                document_id=str(document_id),
                error=f"{type(e).__name__}: {e}",
            )
            return None
        return bool(
            {
                str(document_id),
                metadata_anchor_id(document_id),
                analysis_anchor_id(document_id),
            }
            & corrected
        )

    async def _verify_file(
        self,
        user: "AuthenticatedUser",
        document_id: uuid.UUID,
        *,
        tamper_report: bool,
    ) -> DocumentVerificationResponse:
        """Verify a document against its on-chain anchor, if it has one.

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

        common: dict[str, Any] = dict(
            document_id=document_id,
            file_name=document.file_name,
            finalized_at=document.finalized_at,
            verified_at=datetime.now(timezone.utc),
        )

        # `lifecycle` is a database value an attacker can edit, so it never
        # decides whether the chain is asked. It only sets how hard to ask: a
        # document that claims to be anchored is worth waiting for a lagging node.
        finalized = document.lifecycle == LIFECYCLE_FINALIZED
        try:
            record: ChainRecord | None = await self.provider.get_document_hash(
                document_id=str(document_id), retry_empty=finalized
            )
        except ServiceUnavailableError as e:
            log.error(
                "blockchain.verify.chain_unavailable",
                document_id=str(document_id),
                error=f"{type(e).__name__}: {e}",
            )
            return DocumentVerificationResponse(
                status=VERIFY_UNAVAILABLE,
                is_authentic=False,
                baseline_trusted=False,
                message=(
                    "Could not read the blockchain to verify this document. This "
                    "is NOT a tamper result; the document's integrity is unknown. "
                    "Try again once the chain is reachable."
                ),
                **common,
            )

        if record is None:
            if finalized:
                raise NotFoundError("No on-chain record found for this document")
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

        if not finalized:
            log.warning(
                "blockchain.verify.lifecycle_mismatch",
                document_id=str(document_id),
                lifecycle=document.lifecycle,
            )
        onchain_hash = normalise_hash(record.data_hash)

        snapshot = await self.snapshot_repo.get_by_document_id(document_id)
        if not snapshot:
            if finalized:
                raise NotFoundError("No stored original found for this document")
            # Finalize anchors before it commits, so a failed one leaves this
            # with no attack at all; so does editing `lifecycle` and deleting the
            # snapshot. With no baseline neither can be told apart, so the verdict
            # claims nothing either way. The chain reconcile reports these.
            return DocumentVerificationResponse(
                status=VERIFY_UNAVAILABLE,
                is_authentic=False,
                baseline_trusted=False,
                onchain_hash=onchain_hash,
                onchain_timestamp=record.timestamp,
                issued_by=record.issued_by,
                message=(
                    "A hash is anchored on-chain for this document, but it is not "
                    "finalized and has no frozen record to check against. Finalizing "
                    "may have failed, or these records were changed. The document's "
                    "integrity is unknown."
                ),
                **common,
            )
        baseline_trusted = snapshot.file_sha256 == onchain_hash

        # The contract read carries no tx hash; it was stored when anchoring.
        anchored = await self.onchain_repo.get_by_document_id(document_id)

        chain: dict[str, Any] = dict(
            onchain_hash=onchain_hash,
            snapshot_hash=snapshot.file_sha256,
            tx_hash=record.tx_hash or (anchored.tx_hash if anchored else None),
            onchain_timestamp=record.timestamp,
            issued_by=record.issued_by,
        )

        current: bytes | None = None
        if copy is not None:
            try:
                # Never through the CDN: a cached copy would be compared against
                # the chain and produce a confident, wrong verdict.
                current = await get_storage_client().download(copy.storage_url)
            except FileNotFoundError:
                pass  # Storage answered: the file is gone. Judged below.
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
                        "tamper result; the document's integrity is unknown. Try "
                        "again once storage is available."
                    ),
                    **common,
                    **chain,
                )

        current_hash = hashlib.sha256(current).hexdigest() if current is not None else None

        if current_hash == onchain_hash:
            log.info("blockchain.verify.authentic", document_id=str(document_id))
            # The file is genuine, so the verdict stands; the altered row is
            # reported through `baseline_trusted` and this log.
            if not baseline_trusted:
                log.error(
                    "blockchain.verify.snapshot_altered",
                    document_id=str(document_id),
                    onchain_hash=onchain_hash,
                    snapshot_hash=snapshot.file_sha256,
                )
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
                    (
                        "The stored file does not match the chain"
                        if current is not None
                        else "The stored file is missing"
                    )
                    + ", and the frozen record in the database was altered as well."
                ),
                **common,
                **chain,
            )

        if current is None:
            log.error("blockchain.verify.file_missing", document_id=str(document_id))
            return DocumentVerificationResponse(
                status=VERIFY_FILE_MISSING,
                is_authentic=False,
                baseline_trusted=True,
                message="The stored file is missing, or its record was removed.",
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
            if _engine(original_result) != _engine(current_result):
                # The engine fell back between the two reads; read the original
                # again on the one that read the current copy.
                original_result = await ocr.extract(original)
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


def _engine(result: ExtractionResult) -> str | None:
    return (result.meta or {}).get("engine")
