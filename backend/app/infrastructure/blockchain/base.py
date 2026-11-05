from typing import Protocol

from pydantic import BaseModel


class ChainRecord(BaseModel):
    document_id: str
    data_hash: str
    timestamp: int
    issued_by: str
    tx_hash: str


class BlockchainProvider(Protocol):
    async def store_document_hash(
        self, document_id: str, data_hash: str
    ) -> ChainRecord: ...

    async def get_document_hash(
        self,
        document_id: str,
        retry_empty: bool = True,
    ) -> ChainRecord | None: ...
