import json
from pathlib import Path

from app.core import settings
from ens.constants import HexStr
from web3 import AsyncWeb3
from web3.types import TxReceipt

from .base import ChainRecord


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
        self, document_id: str, data_hash: HexStr
    ) -> ChainRecord:
        data_hash_bytes32 = self.w3.to_bytes(hexstr=data_hash)
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

        (
            data_hash_bytes32,
            timestamp,
            issued_by,
        ) = await self._contract.functions.getDocumentHash(document_id).call()

        return ChainRecord(
            document_id=document_id,
            data_hash=data_hash_bytes32.hex(),
            timestamp=timestamp,
            issued_by=issued_by,
            tx_hash=receipt["transactionHash"].hex(),
        )

    async def get_document_hash(self, document_id: str) -> ChainRecord | None:
        try:
            (
                data_hash,
                timestamp,
                issued_by,
            ) = await self._contract.functions.getDocumentHash(document_id).call()
            if timestamp == 0:
                return None
            return ChainRecord(
                document_id=document_id,
                data_hash=data_hash.hex(),
                timestamp=timestamp,
                issued_by=issued_by,
                tx_hash="",
            )
        except Exception:
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
