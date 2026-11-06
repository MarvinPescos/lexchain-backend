import asyncio
import json
from pathlib import Path

from app.core import settings
from app.shared.errors.exceptions import BadGatewayError, ServiceUnavailableError
from ens.constants import HexStr
from web3 import AsyncWeb3
from web3.types import TxReceipt

from .base import ChainRecord, OwnerEvent

# A just-mined anchor can read as empty on a lagging RPC node.
_EMPTY_READ_RETRIES = 3
_EMPTY_READ_DELAY_SECONDS = 2

_OWNER_EVENTS = ("DocumentRecordCorrected", "RecorderSet", "OwnershipTransferred")


class _CorrectionScan:
    """How far `corrected_document_ids` has read, per contract, for this process."""

    def __init__(self, next_block: int) -> None:
        self.next_block = next_block
        self.document_ids: set[str] = set()


_correction_scans: dict[str, _CorrectionScan] = {}


class Web3BlockchainProvider:
    def __init__(self, contract_address: str | None = None) -> None:
        self.w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(settings.WEB_RPC_URL))
        self.contract_address = contract_address or settings.CONTRACT_ADDRESS
        self.private_key = settings.CONTRACT_OWNER_PRIVATE_KEY

        self.abi: list[dict] = json.loads(_abi_path().read_text())["abi"]
        self.account = self.w3.eth.account.from_key(self.private_key)

    @property
    def recorder_address(self) -> str:
        return self.account.address

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

    async def get_document_hash(
        self, document_id: str, retry_empty: bool = True
    ) -> ChainRecord | None:
        """The anchored record, or None if the chain has none.

        Callers ask about documents they anchored, so an empty answer is
        retried briefly: a load-balanced RPC can serve a node that hasn't seen
        the block yet. `retry_empty=False` is for documents not expected to be
        anchored, where the empty answer is the usual one. An unreachable chain
        raises instead of passing for "never anchored".
        """
        retries = _EMPTY_READ_RETRIES if retry_empty else 0
        for attempt in range(retries + 1):
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
            if attempt < retries:
                await asyncio.sleep(_EMPTY_READ_DELAY_SECONDS)
        return None

    async def list_anchored_document_ids(self, from_block: int) -> set[str]:
        """Every document ID anchored from `from_block` to the latest block.

        Read in chunks, because RPC providers cap the `get_logs` range. Any
        failed chunk raises: a partial list must never pass for a complete one.
        """
        event = self._contract.events.DocumentRecordVerified()
        chunk_size = settings.CHAIN_LOG_CHUNK_BLOCKS
        document_ids: set[str] = set()
        try:
            latest = await self.w3.eth.block_number
            if from_block > latest:
                raise ValueError(f"Block {from_block} is past the latest, {latest}")
            for start in range(from_block, latest + 1, chunk_size):
                logs = await event.get_logs(
                    from_block=start, to_block=min(start + chunk_size - 1, latest)
                )
                document_ids.update(log["args"]["documentId"] for log in logs)
        except Exception as e:
            raise ServiceUnavailableError(
                "The blockchain could not be read to list the anchored documents."
            ) from e
        return document_ids

    async def list_owner_events(self, from_block: int) -> list[OwnerEvent]:
        """Every use of the owner key from `from_block` to the latest block,
        oldest first. Any failed chunk raises, like `list_anchored_document_ids`.
        """
        chunk_size = settings.CHAIN_LOG_CHUNK_BLOCKS
        events: list[OwnerEvent] = []
        try:
            latest = await self.w3.eth.block_number
            if from_block > latest:
                raise ValueError(f"Block {from_block} is past the latest, {latest}")
            for start in range(from_block, latest + 1, chunk_size):
                events += await self._owner_events(
                    start, min(start + chunk_size - 1, latest)
                )
        except Exception as e:
            raise ServiceUnavailableError(
                "The blockchain could not be read to list the owner key's events."
            ) from e
        return events

    async def corrected_document_ids(self, from_block: int) -> set[str]:
        """The IDs whose anchor was replaced, for verify.

        Verify runs on every open, and a full scan takes minutes, so this keeps
        its place in memory and reads only what's new. Progress survives a
        caller that gives up, so a cold process catches up over a few calls.
        The scheduled watcher doesn't use this: it rescans everything.
        """
        scan = _correction_scans.setdefault(
            self.contract_address, _CorrectionScan(from_block)
        )
        chunk_size = settings.CHAIN_LOG_CHUNK_BLOCKS
        try:
            latest = await self.w3.eth.block_number
            while scan.next_block <= latest:
                end = min(scan.next_block + chunk_size - 1, latest)
                events = await self._owner_events(scan.next_block, end)
                scan.document_ids.update(
                    e.document_id for e in events if e.document_id is not None
                )
                scan.next_block = end + 1
        except Exception as e:
            raise ServiceUnavailableError(
                "The blockchain could not be read to list corrected anchors."
            ) from e
        return set(scan.document_ids)

    async def _owner_events(self, start: int, end: int) -> list[OwnerEvent]:
        """One `get_logs` for all three events, decoded in chain order."""
        contract = self._contract
        decoders = {
            event.topic: event
            for event in (contract.events[name]() for name in _OWNER_EVENTS)
        }
        logs = await self.w3.eth.get_logs(
            {
                "address": contract.address,
                "fromBlock": start,
                "toBlock": end,
                "topics": [list(decoders)],
            }
        )
        return [
            _owner_event(decoders[self.w3.to_hex(log["topics"][0])].process_log(log))
            for log in logs
        ]


def _owner_event(log) -> OwnerEvent:
    args = log["args"]
    common = {"tx_hash": log["transactionHash"].hex(), "block_number": log["blockNumber"]}
    if log["event"] == "DocumentRecordCorrected":
        return OwnerEvent(
            kind="correction",
            document_id=args["documentId"],
            previous_hash=args["previousHash"].hex(),
            new_hash=args["dataHash"].hex(),
            reason=args["reason"],
            **common,
        )
    if log["event"] == "RecorderSet":
        return OwnerEvent(
            kind="recorder_set",
            recorder=args["recorder"],
            authorized=args["authorized"],
            **common,
        )
    return OwnerEvent(
        kind="ownership_transferred",
        previous_owner=args["previousOwner"],
        new_owner=args["newOwner"],
        **common,
    )


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
