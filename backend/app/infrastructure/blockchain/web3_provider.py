import asyncio
import json
from pathlib import Path

from app.core import settings
from app.shared.errors.exceptions import BadGatewayError, ServiceUnavailableError
from ens.constants import HexStr
from web3 import AsyncWeb3
from web3.types import TxReceipt

from .base import ChainRecord

# A just-mined anchor can read as empty on a lagging RPC node.
_EMPTY_READ_RETRIES = 3
_EMPTY_READ_DELAY_SECONDS = 2


class Web3BlockchainProvider:
    def __init__(self) -> None:
        self.w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(settings.WEB_RPC_URL))
        self.contract_address = settings.CONTRACT_ADDRESS
        self.private_key = settings.CONTRACT_OWNER_PRIVATE_KEY

        self.abi: list[dict] = json.loads(_abi_path().read_text())["abi"]
        self.account = self.w3.eth.account.from_key(self.private_key)

    @property
    def _contract(self):
        return self.w3.eth.contract(address=self.contract_address, abi=self.abi)  # type: ignore

    async def store_document_hash(
        self, document_id: str, data_hash: str
    ) -> ChainRecord:
        data_hash_bytes32 = self.w3.to_bytes(hexstr=HexStr(data_hash))
        tx = await self._contract.functions.storeDocumentHash(
            document_id, data_hash_bytes32
        ).build_transaction(
            {
                "from": self.account.address,
                "nonce": await self.w3.eth.get_transaction_count(self.account.address),
            }
        )

        signed = self.account.sign_transaction(tx)
        tx_hash = await self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt: TxReceipt = await self.w3.eth.wait_for_transaction_receipt(tx_hash)

        if receipt["status"] != 1:
            raise BadGatewayError(
                "The blockchain rejected the anchor transaction; nothing was finalized.",
                details={"tx_hash": receipt["transactionHash"].hex()},
            )

        # Read the stored values from the mined transaction's own event, not
        # with getDocumentHash: a load-balanced RPC can answer that read from a
        # node that hasn't seen the block yet, which returned all zeros.
        events = self._contract.events.DocumentRecordVerified().process_receipt(receipt)
        event = next((e for e in events if e["args"]["documentId"] == document_id), None)
        if event is None or event["args"]["dataHash"] != data_hash_bytes32:
            raise BadGatewayError(
                "The anchor transaction did not record the expected hash.",
                details={"tx_hash": receipt["transactionHash"].hex()},
            )

        return ChainRecord(
            document_id=document_id,
            data_hash=event["args"]["dataHash"].hex(),
            timestamp=event["args"]["timestamp"],
            # msg.sender is the signing account.
            issued_by=self.account.address,
            tx_hash=receipt["transactionHash"].hex(),
        )

    async def get_document_hash(self, document_id: str) -> ChainRecord | None:
        """The anchored record, or None if the chain has none.

        Callers ask about documents they anchored, so an empty answer is
        retried briefly: a load-balanced RPC can serve a node that hasn't seen
        the block yet. An unreachable chain raises instead of passing for
        "never anchored".
        """
        for attempt in range(_EMPTY_READ_RETRIES + 1):
            try:
                (
                    data_hash,
                    timestamp,
                    issued_by,
                ) = await self._contract.functions.getDocumentHash(document_id).call()
            except Exception as e:
                raise ServiceUnavailableError(
                    "The blockchain could not be reached to read the anchor."
                ) from e
            if timestamp != 0:
                return ChainRecord(
                    document_id=document_id,
                    data_hash=data_hash.hex(),
                    timestamp=timestamp,
                    issued_by=issued_by,
                    tx_hash="",
                )
            if attempt < _EMPTY_READ_RETRIES:
                await asyncio.sleep(_EMPTY_READ_DELAY_SECONDS)
        return None


def _abi_path() -> Path:
    """The compiled contract's artifact.

    In Docker, compose mounts the Foundry project at <backend>/blockchain; run
    on the host, it sits next to backend/ in the repo. First one that exists.
    """
    backend = Path(__file__).resolve().parents[3]
    artifact = Path("out") / "LexChainNotary.sol" / "LexChainNotary.json"
    candidates = [backend / "blockchain" / artifact, backend.parent / "blockchain" / artifact]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(
        "LexChainNotary ABI not found — run `forge build` in blockchain/. Looked in: "
        + ", ".join(str(p) for p in candidates)
    )
