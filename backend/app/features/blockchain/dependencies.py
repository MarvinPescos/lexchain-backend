from app.core import settings
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.blockchain.mock_provider import MockBlockchainProvider
from app.infrastructure.blockchain.web3_provider import Web3BlockchainProvider


def get_blockchain_provider() -> BlockchainProvider:
    # Every deployed environment, staging included, anchors for real.
    if settings.ENVIRONMENT == "test":
        return MockBlockchainProvider()
    return Web3BlockchainProvider()
