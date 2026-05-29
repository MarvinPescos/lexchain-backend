from app.core import settings
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.blockchain.mock_provider import MockBlockchainProvider
from app.infrastructure.blockchain.web3_provider import Web3BlockchainProvider
from app.infrastructure.database import get_db
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from .service import BlockchainService


def get_blockchain_provider() -> BlockchainProvider:
    if settings.ENVIRONMENT == "development":
        return Web3BlockchainProvider()
    return MockBlockchainProvider()


def get_blockchain_service(
    db: AsyncSession = Depends(get_db),
    provider: BlockchainProvider = Depends(get_blockchain_provider),
) -> BlockchainService:
    return BlockchainService(db, provider)
