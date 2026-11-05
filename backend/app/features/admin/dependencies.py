from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.infrastructure.database import get_db
from .service import AdminService
from app.infrastructure.email import build_email_provider, EmailProvider
from app.infrastructure.backup import build_backup
from app.features.snapshot.backups import SnapshotArchive
from app.features.blockchain.dependencies import get_blockchain_provider
from app.features.blockchain.reconciliation import ChainReconciliation
from app.infrastructure.blockchain import BlockchainProvider


def get_email_provider() -> EmailProvider:
    return build_email_provider()


def get_admin_service(
    db: AsyncSession = Depends(get_db),
    email: EmailProvider = Depends(get_email_provider),
) -> AdminService:
    return AdminService(db, email)


def get_snapshot_archive(
    db: AsyncSession = Depends(get_db),
    provider: BlockchainProvider = Depends(get_blockchain_provider),
) -> SnapshotArchive:
    return SnapshotArchive(db, build_backup(), provider)


def get_chain_reconciliation(
    db: AsyncSession = Depends(get_db),
    provider: BlockchainProvider = Depends(get_blockchain_provider),
) -> ChainReconciliation:
    return ChainReconciliation(db, provider)
