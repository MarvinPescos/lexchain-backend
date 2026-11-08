"""Tamper reports: every document with a problem still open, and one action to
fix them all. A deleted document is one of them, listed from its deletion record.

History only says which documents to look at. Each one is checked again here, so
a report never shows a problem that has been fixed, and the changes it lists are
true now rather than when they were found.
"""

import uuid
from datetime import datetime

from app.features.blockchain.constants import (
    VERIFY_AUTHENTIC,
    VERIFY_FILE_MISSING,
    VERIFY_TAMPER_FINDINGS,
    VERIFY_UNAVAILABLE,
)
from app.features.blockchain.repository import VerificationResultRepository
from app.features.blockchain.schemas import DocumentVerificationResponse
from app.features.snapshot.records import document_hash_change
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.errors.exceptions import (
    BaseAppException,
    NotFoundError,
    ServiceUnavailableError,
)

from ..constants import LIFECYCLE_FINALIZED
from ..models import DeletedDocument, Document
from ..repository import DeletedDocumentRepository
from ..schemas import (
    RestoreAllResponse,
    RestoreFailure,
    TamperChange,
    TamperReportItem,
    TamperReportListResponse,
)
from .recovery import DocumentRecovery
from .restoration import DocumentRestoration

STATUS_DELETED = "DELETED"


class TamperReports:
    def __init__(self, restoration: DocumentRestoration) -> None:
        self.restoration = restoration
        self.session = restoration.session
        self.verifier = restoration.verifier
        self.records = restoration.records
        self.snapshot_repo = restoration.snapshot_repo
        self.findings = VerificationResultRepository(restoration.session)
        self.deletions = DeletedDocumentRepository(restoration.session)
        self.recovery = DocumentRecovery(restoration.session)

    async def list_open(self, user: AuthenticatedUser) -> TamperReportListResponse:
        reports: list[TamperReportItem] = []
        for document_id, detected_at in await self.findings.open_findings_for_owner(
            user.user_id
        ):
            report = await self._report(user, document_id, detected_at)
            if report is not None:
                reports.append(report)
        reports.extend(
            self._deleted(d)
            for d in await self.deletions.list_open_for_owner(user.user_id)
        )
        reports.sort(key=lambda r: r.detected_at, reverse=True)
        return TamperReportListResponse(total=len(reports), reports=reports)

    async def restore_all(self, user: AuthenticatedUser) -> RestoreAllResponse:
        result = RestoreAllResponse()
        for report in (await self.list_open(user)).reports:
            try:
                if report.status == STATUS_DELETED:
                    result.recovered.append(
                        await self.recovery.recover(user, report.document_id)
                    )
                else:
                    result.restored.append(
                        await self.restoration.restore(user, report.document_id)
                    )
            except BaseAppException as e:
                await self.session.rollback()
                log.error(
                    "document.tamper_reports.restore_failed",
                    document_id=str(report.document_id),
                    error=e.message,
                )
                result.failed.append(
                    RestoreFailure(
                        document_id=report.document_id,
                        file_name=report.file_name,
                        message=e.message,
                    )
                )
        return result

    async def _report(
        self, user: AuthenticatedUser, document_id: uuid.UUID, detected_at: datetime
    ) -> TamperReportItem | None:
        """The document's open problem, or None if there is nothing to report."""
        document = await self.session.get(Document, document_id)
        assert document is not None  # the finding's foreign key
        file_name = document.file_name
        try:
            verdict = await self.verifier.verify_document(
                user, document_id, tamper_report=False
            )
        except NotFoundError:
            # No anchor to judge it by, so nothing here can be fixed.
            log.warning("document.tamper_reports.no_anchor", document_id=str(document_id))
            return None
        except ServiceUnavailableError:
            return self._unknown(document_id, file_name, detected_at, VERIFY_UNAVAILABLE)

        # A problem is never dropped just because it couldn't be read.
        if verdict.status == VERIFY_UNAVAILABLE:
            return self._unknown(document_id, file_name, detected_at, verdict.status)
        metadata_unread = verdict.metadata_status == "UNAVAILABLE"
        analysis_unread = verdict.analysis_status == "UNAVAILABLE"
        if not verdict.needs_restore and not (metadata_unread or analysis_unread):
            return None

        archived = None
        if verdict.onchain_hash is not None:
            archived = await self.records.archived_pdf(document_id, verdict.onchain_hash)
        changes = await self._changes(document, verdict, archived)

        needs_archive = verdict.status != VERIFY_AUTHENTIC or any(
            c.kind == "snapshot" and c.field == "file_sha256" for c in changes
        )
        blocked = None
        if metadata_unread:
            blocked = "metadata_unreadable"
        elif analysis_unread:
            blocked = "analysis_unreadable"
        elif needs_archive and archived is None:
            blocked = "archive_missing"
        return TamperReportItem(
            document_id=document_id,
            file_name=file_name,
            detected_at=detected_at,
            status=verdict.status,
            changes=changes,
            restorable=blocked is None,
            blocked_reason=blocked,
        )

    async def _changes(
        self,
        document: Document,
        verdict: DocumentVerificationResponse,
        archived: bytes | None,
    ) -> list[TamperChange]:
        changes: list[TamperChange] = []
        if verdict.status in VERIFY_TAMPER_FINDINGS:
            changes.append(
                TamperChange(
                    kind="file",
                    field="file",
                    genuine=verdict.onchain_hash,
                    current=(
                        None
                        if verdict.status == VERIFY_FILE_MISSING
                        else verdict.current_hash
                    ),
                )
            )
        if document.lifecycle != LIFECYCLE_FINALIZED:
            changes.append(
                TamperChange(
                    kind="lifecycle",
                    field="lifecycle",
                    genuine=LIFECYCLE_FINALIZED,
                    current=document.lifecycle,
                )
            )
        changes.extend(
            TamperChange(
                kind="metadata", field=c.field, genuine=c.anchored, current=c.current
            )
            for c in verdict.metadata_changes
        )
        changes.extend(
            TamperChange(
                kind="analysis", field=c.field, genuine=c.anchored, current=c.current
            )
            for c in verdict.analysis_changes
        )
        hash_change = (
            document_hash_change(document, verdict.onchain_hash)
            if verdict.onchain_hash is not None
            else None
        )
        if hash_change is not None:
            changes.append(
                TamperChange(
                    kind="document",
                    field=hash_change.field,
                    genuine=hash_change.anchored,
                    current=hash_change.current,
                )
            )
        snapshot = await self.snapshot_repo.get_by_document_id(document.id)
        if snapshot is not None and verdict.onchain_hash is not None:
            changes.extend(
                TamperChange(
                    kind="snapshot", field=c.field, genuine=c.anchored, current=c.current
                )
                for c in await self.records.snapshot_changes(
                    snapshot, verdict.onchain_hash, archived
                )
            )
        return changes

    @staticmethod
    def _deleted(deleted: DeletedDocument) -> TamperReportItem:
        saved = deleted.document is not None
        return TamperReportItem(
            document_id=deleted.document_id,
            file_name=deleted.file_name,
            detected_at=deleted.deleted_at,
            status=STATUS_DELETED,
            changes=[
                TamperChange(
                    kind="deleted", field="document", genuine=deleted.file_name
                )
            ],
            restorable=saved,
            blocked_reason=None if saved else "backup_needed",
        )

    @staticmethod
    def _unknown(
        document_id: uuid.UUID,
        file_name: str | None,
        detected_at: datetime,
        status: str,
    ) -> TamperReportItem:
        return TamperReportItem(
            document_id=document_id,
            file_name=file_name,
            detected_at=detected_at,
            status=status,
            restorable=False,
            blocked_reason="check_unavailable",
        )
