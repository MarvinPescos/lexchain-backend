import asyncio 
import json
from pathlib import Path

from web3 import AsyncWeb3
from web3.types import TxReceipt

from .base import ChainRecord
from app.core import settings

class Web3BlockchainProvider:
    def __init__(self) -> None:
        self.w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(settings.WEB_RPC_URL))
        self.contract_address = settings.CONTRACT_ADDRESS
        self.private_key = settings.CONTRACT_OWNER_PRIVATE_KEY

        abi_path = Path(__file__).resolve().parent.parent.parent.parent / "blockchain" / "out" / "LexChainNotary.sol" / "LexChainNotary.json" 
        self.abi: list[dict] = json.loads(abi_path.read_text())["abi"]
        self.account = self.w3.eth.account.from_key(self.private_key)

    @property
    def _contract(self):
        return self.w3.eth.contract(address=self.contract_address, abi=self.abi)
    
    async def store_document_hash(self, document_id: str, data_hash: str) -> ChainRecord:
        data_hash_bytes32 = self.w3.to_bytes(hexstr=data_hash)
        tx = await self._contract.functions.storeDocumentHash(
            document_id, data_hash_bytes32
        ).build_transaction({
            "from": self.account.address,
            "nonce": await self.w3.eth.get_transaction_count(self.account.address),
        })

        signed = self.account.sign_transaction(tx)
        tx_hash = await self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt: TxReceipt = await self.w3.eth.wait_for_transaction_receipt(tx_hash)

        data_hash_bytes32, timestamp, issued_by = await self._contract.functions.getDocumentHash(document_id).call()

        return ChainRecord(
            document_id=document_id,
            data_hash=data_hash_bytes32.hex(),
            timestamp=timestamp,
            issued_by=issued_by,
            tx_hash=receipt.transactionHash.hex()
        )
    
    async def get_document_hash(self, document_id: str) -> ChainRecord | None: 
        try:
            data_hash, timestamp, issued_by = await self.contract.functions.getDocumentHash(document_id).call()
            if timestamp == 0:
                return None
            return ChainRecord(
                document_id=document_id,
                data_hash=data_hash,
                timestamp=timestamp,
                issued_by=issued_by,
                tx_hash=""
            )
        except Exception:
            return None
    

