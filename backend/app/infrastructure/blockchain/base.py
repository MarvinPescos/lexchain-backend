from typing import Literal, Protocol

from pydantic import BaseModel


class ChainRecord(BaseModel):
    document_id: str
    data_hash: str
    timestamp: int
    issued_by: str
    tx_hash: str


class OwnerEvent(BaseModel):
    """A use of the contract owner key, read from the chain's events."""

    kind: Literal["correction", "recorder_set", "ownership_transferred"]
    tx_hash: str
    block_number: int
    # correction
    document_id: str | None = None
    previous_hash: str | None = None
    new_hash: str | None = None
    reason: str | None = None
    # recorder_set
    recorder: str | None = None
    authorized: bool | None = None
    # ownership_transferred
    previous_owner: str | None = None
    new_owner: str | None = None


class BlockchainProvider(Protocol):
    @property
    def recorder_address(self) -> str:
        """The key this app anchors with."""
        ...

    async def store_document_hash(
        self, document_id: str, data_hash: str
    ) -> ChainRecord: ...

    async def get_document_hash(
        self,
        document_id: str,
        retry_empty: bool = True,
    ) -> ChainRecord | None: ...

    async def list_anchored_document_ids(self, from_block: int) -> set[str]: ...

    async def list_owner_events(self, from_block: int) -> list[OwnerEvent]: ...

    async def corrected_document_ids(self, from_block: int) -> set[str]: ...
