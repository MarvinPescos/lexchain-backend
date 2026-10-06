from app.core import settings
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.blockchain.mock_provider import MockBlockchainProvider
from app.infrastructure.blockchain.web3_provider import Web3BlockchainProvider


def get_blockchain_provider() -> BlockchainProvider:
    # Deliberate: dev runs against the real chain so Web3 gets exercised.
    if settings.ENVIRONMENT == "development":
        return Web3BlockchainProvider()
    return MockBlockchainProvider()
