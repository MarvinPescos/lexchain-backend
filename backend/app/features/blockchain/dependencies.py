from app.core import settings
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.blockchain.mock_provider import MockBlockchainProvider
from app.infrastructure.blockchain.web3_provider import Web3BlockchainProvider


def get_blockchain_provider() -> BlockchainProvider:
    # Dev runs against the real chain too, so Web3 gets exercised.
    if settings.ENVIRONMENT in ("development", "production"):
        return Web3BlockchainProvider()
    return MockBlockchainProvider()
