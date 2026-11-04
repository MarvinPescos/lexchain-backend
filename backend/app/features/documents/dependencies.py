from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.features.blockchain.dependencies import get_blockchain_provider
from app.infrastructure.backup import build_backup
from app.infrastructure.blockchain import BlockchainProvider
from app.features.google.service import GoogleConnectionService
from app.infrastructure.database import get_db
from app.infrastructure.drafts import build_draft_workspace
from app.infrastructure.email import build_email_provider
from app.infrastructure.google import build_google_oauth
from app.infrastructure.queue import (
    CeleryDocumentQueue,
    DocumentQueue,
    LoggingDocumentQueue,
)

from .queries import DocumentQueries
from .workflow.discussion import DocumentDiscussion
from .workflow.drafting import DocumentDrafting
from .workflow.ecopy import DocumentEcopy
from .workflow.finalization import DocumentFinalization
from .workflow.intake import DocumentIntake
from .workflow.processing import DocumentProcessing
from .workflow.restoration import DocumentRestoration
from .workflow.sharing import DocumentSharing


def get_document_queue() -> DocumentQueue:
    """Dependency injection for queue provider."""
    # Use CeleryDocumentQueue if broker URL is configured or we're in production
    if settings.CELERY_BROKER_URL or settings.ENVIRONMENT == "production":
        return CeleryDocumentQueue()
    return LoggingDocumentQueue()


def get_intake(
    db: AsyncSession = Depends(get_db),
    queue: DocumentQueue = Depends(get_document_queue),
) -> DocumentIntake:
    return DocumentIntake(db, queue)


def get_queries(db: AsyncSession = Depends(get_db)) -> DocumentQueries:
    return DocumentQueries(db)


def get_drafting(db: AsyncSession = Depends(get_db)) -> DocumentDrafting:
    return DocumentDrafting(
        db, build_draft_workspace(), GoogleConnectionService(db, build_google_oauth())
    )


def get_discussion(db: AsyncSession = Depends(get_db)) -> DocumentDiscussion:
    return DocumentDiscussion(
        db, build_draft_workspace(), GoogleConnectionService(db, build_google_oauth())
    )


def get_sharing(
    db: AsyncSession = Depends(get_db),
    drafting: DocumentDrafting = Depends(get_drafting),
) -> DocumentSharing:
    return DocumentSharing(db, drafting)


def get_finalization(
    db: AsyncSession = Depends(get_db),
    provider: BlockchainProvider = Depends(get_blockchain_provider),
) -> DocumentFinalization:
    backup = build_backup() if settings.BACKUP_ENABLED else None
    return DocumentFinalization(db, provider, backup)


def get_ecopy(db: AsyncSession = Depends(get_db)) -> DocumentEcopy:
    return DocumentEcopy(db, build_email_provider())


def get_processing(
    db: AsyncSession = Depends(get_db),
    queue: DocumentQueue = Depends(get_document_queue),
) -> DocumentProcessing:
    return DocumentProcessing(db, queue)


def get_restoration(
    db: AsyncSession = Depends(get_db),
    provider: BlockchainProvider = Depends(get_blockchain_provider),
) -> DocumentRestoration:
    backup = build_backup() if settings.BACKUP_ENABLED else None
    return DocumentRestoration(db, provider, backup)


def get_verification(
    db: AsyncSession = Depends(get_db),
    provider: BlockchainProvider = Depends(get_blockchain_provider),
):
    # Imported here: blockchain.service imports the documents package.
    from app.features.blockchain.service import BlockchainService

    backup = build_backup() if settings.BACKUP_ENABLED else None
    return BlockchainService(db, provider, backup)


def get_integrity_report(db: AsyncSession = Depends(get_db)):
    # Imported here: blockchain.report imports the documents package.
    from app.features.blockchain.report import IntegrityReport

    return IntegrityReport(db)
