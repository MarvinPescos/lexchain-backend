import asyncio

from .base import ChainRecord


class MockBlockchainProvider:
    async def store_document_hash(
        self, document_id: str, data_hash: str
    ) -> ChainRecord:
        await asyncio.sleep(0.1)
        return ChainRecord(
            document_id=document_id,
            data_hash=data_hash,
            timestamp=0,
            issued_by="0x000000000000000000000000000000",
            tx_hash="0x" + "00" * 32,
        )

    async def get_document_hash(
        self, document_id: str, retry_empty: bool = True
    ) -> ChainRecord | None:
        return None

    async def list_anchored_document_ids(self, from_block: int) -> set[str]:
        return set()
