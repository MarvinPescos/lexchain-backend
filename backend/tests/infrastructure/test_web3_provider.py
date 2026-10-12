"""Web3 provider against a fake contract: the two failures seen live on
Base Sepolia (a lagging RPC node reading an anchor back as empty) and the
missing receipt-status check."""

import pytest

from app.infrastructure.blockchain import web3_provider
from app.infrastructure.blockchain.web3_provider import Web3BlockchainProvider
from app.shared.errors.exceptions import BadGatewayError, ServiceUnavailableError

HASH = "ab" * 32


class _Call:
    def __init__(self, result):
        self.result = result

    async def call(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result.pop(0) if isinstance(self.result, list) else self.result


class _Tx:
    async def build_transaction(self, params):
        return {}


class FakeContract:
    def __init__(self, reads=None, status=1, event_hash=bytes.fromhex(HASH)):
        self.reads = reads
        self.status = status
        self.event_hash = event_hash
        outer = self

        class Functions:
            def storeDocumentHash(self, document_id, data_hash):
                return _Tx()

            def getDocumentHash(self, document_id):
                return _Call(outer.reads)

        class Event:
            def process_receipt(self, receipt):
                return [{"args": {"documentId": "d1", "dataHash": outer.event_hash, "timestamp": 1700}}]

        class Events:
            def DocumentRecordVerified(self):
                return Event()

        self.functions = Functions()
        self.events = Events()


def provider(contract: FakeContract, status: int = 1) -> Web3BlockchainProvider:
    class Provider(Web3BlockchainProvider):
        _contract = property(lambda self: contract)

    p = Provider.__new__(Provider)

    class Account:
        address = "0xLawyerKey"

        def sign_transaction(self, tx):
            return type("Signed", (), {"raw_transaction": b"raw"})()

    class Eth:
        async def get_transaction_count(self, address):
            return 0

        async def send_raw_transaction(self, raw):
            return b"tx"

        async def wait_for_transaction_receipt(self, tx_hash):
            return {"status": status, "transactionHash": bytes.fromhex("cd" * 32)}

    class W3:
        eth = Eth()

        def to_bytes(self, hexstr):
            return bytes.fromhex(hexstr)

    p.w3, p.account = W3(), Account()
    return p


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    async def instant(seconds):
        return None

    monkeypatch.setattr(web3_provider.asyncio, "sleep", instant)


async def test_anchor_values_come_from_the_event_not_a_read_back():
    # The read-back would return zeros, as a lagging node did live.
    p = provider(FakeContract(reads=(b"\0" * 32, 0, "0x0")))
    record = await p.store_document_hash("d1", HASH)
    assert (record.data_hash, record.timestamp, record.issued_by) == (HASH, 1700, "0xLawyerKey")


async def test_reverted_anchor_is_refused():
    with pytest.raises(BadGatewayError, match="rejected"):
        await provider(FakeContract(), status=0).store_document_hash("d1", HASH)


async def test_event_with_another_hash_is_refused():
    with pytest.raises(BadGatewayError, match="expected hash"):
        await provider(FakeContract(event_hash=b"\1" * 32)).store_document_hash("d1", HASH)


async def test_empty_read_is_retried_until_the_node_catches_up():
    reads = [(b"\0" * 32, 0, "0x0"), (b"\0" * 32, 0, "0x0"), (bytes.fromhex(HASH), 1700, "0xA")]
    record = await provider(FakeContract(reads=reads)).get_document_hash("d1")
    assert record.data_hash == HASH


async def test_never_anchored_is_none():
    p = provider(FakeContract(reads=[(b"\0" * 32, 0, "0x0")] * 4))
    assert await p.get_document_hash("d1") is None


async def test_unreachable_chain_is_not_reported_as_missing():
    p = provider(FakeContract(reads=ConnectionError("rpc down")))
    with pytest.raises(ServiceUnavailableError):
        await p.get_document_hash("d1")
