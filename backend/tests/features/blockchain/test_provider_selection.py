import pytest

from app.core import settings
from app.features.blockchain import dependencies
from app.infrastructure.blockchain.mock_provider import MockBlockchainProvider


@pytest.mark.parametrize("environment", ["development", "production"])
def test_real_environments_use_the_real_chain(monkeypatch, environment):
    class Web3:
        pass

    monkeypatch.setattr(dependencies, "Web3BlockchainProvider", Web3)
    monkeypatch.setattr(settings, "ENVIRONMENT", environment)

    assert isinstance(dependencies.get_blockchain_provider(), Web3)


def test_any_other_environment_uses_the_mock(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "test")

    assert isinstance(dependencies.get_blockchain_provider(), MockBlockchainProvider)
