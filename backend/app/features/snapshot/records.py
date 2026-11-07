"""Checks a finalized document's database records against the chain.

The rule is the PDF's: the chain says what is genuine, and the archive holds the
bytes to get it back. The metadata record is frozen at finalize as canonical
JSON, its hash anchored under `{document_id}:metadata`, and the JSON archived
beside the PDF. Parties aren't in it: sharing is allowed after finalize.

A mismatch is diffed against the archived JSON, and only after that JSON
re-hashes to the anchor. A chain or archive read failure is unknown, never a
finding.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Literal

from app.features.documents.models import Document, SignedCopy
from app.features.users.models import User
from app.infrastructure.backup import BackupProvider
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.observability.logging_setup import log
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DocumentSnapshot
from .schemas import MetadataStatus, RecordChange

METADATA_RECORD_VERSION = 1
METADATA_ANCHOR_SUFFIX = ":metadata"

MetadataProblem = Literal["chain", "archive_missing", "archive_mismatch", "archive_read"]


def normalise_hash(value: str) -> str:
    """Chain reads come back with or without the 0x prefix depending on the provider."""
    return value.lower().removeprefix("0x")


def archive_key(document_id: uuid.UUID, file_hash: str) -> str:
    return f"{document_id}/{file_hash}.pdf"


def metadata_key(document_id: uuid.UUID, metadata_hash: str) -> str:
    return f"{document_id}/metadata-{metadata_hash}.json"


def metadata_anchor_id(document_id: uuid.UUID | str) -> str:
    return f"{document_id}{METADATA_ANCHOR_SUFFIX}"


def is_metadata_anchor(anchored_id: str) -> bool:
    return anchored_id.endswith(METADATA_ANCHOR_SUFFIX)


def document_hash_change(document: Document, onchain_hash: str) -> RecordChange | None:
    """`document_hash` is a working copy of the anchored hash. E-copy access and
    the duplicate check read it, so an edit cuts a client off with a genuine PDF."""
    if document.document_hash == onchain_hash:
        return None
    return RecordChange(
        field="document_hash", anchored=onchain_hash, current=document.document_hash
    )


def metadata_record(document: Document) -> dict[str, Any]:
    """What the app never changes after finalize, in fixed formats."""
    finalized_at = document.finalized_at
    return {
        "book_id": str(document.book_id) if document.book_id else None,
        "doc_no": document.doc_no,
        "file_name": document.file_name,
        "finalized_at": (
            finalized_at.astimezone(timezone.utc).isoformat(timespec="microseconds")
            if finalized_at
            else None
        ),
        "finalized_by": str(document.finalized_by) if document.finalized_by else None,
        "page_no": document.page_no,
        "user_id": str(document.user_id),
        "version": METADATA_RECORD_VERSION,
    }


def canonical_json(record: dict[str, Any]) -> bytes:
    """The same record always gives the same bytes."""
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class MetadataCheck:
    status: MetadataStatus
    changes: list[RecordChange] = field(default_factory=list)
    # The archived record, once proven against the anchor.
    anchored: dict[str, Any] | None = None
    problem: MetadataProblem | None = None


class RecordCheck:
    def __init__(
        self,
        session: AsyncSession,
        chain: BlockchainProvider,
        backup: BackupProvider | None,
    ) -> None:
        self.session = session
        self.chain = chain
        self.backup = backup

    async def metadata(
        self, document: Document, *, check_archive: bool = False
    ) -> MetadataCheck:
        """Rebuild the record from the live row and compare it with the anchor.

        A match needs no archive. `check_archive` reads it anyway, so that a lost
        copy shows up before it's needed: `problem` then says what's wrong with it.
        """
        document_id = str(document.id)
        try:
            anchor = await self.chain.get_document_hash(
                document_id=metadata_anchor_id(document.id), retry_empty=False
            )
        except Exception as e:
            log.error("records.metadata.chain_read_failed", document_id=document_id, error=str(e))
            return MetadataCheck("UNAVAILABLE", problem="chain")
        if anchor is None:
            return MetadataCheck("NOT_ANCHORED")

        anchored_hash = normalise_hash(anchor.data_hash)
        live = metadata_record(document)
        if sha256_hex(canonical_json(live)) == anchored_hash:
            if not check_archive:
                return MetadataCheck("MATCH")
            problem, _ = await self._archived(document.id, anchored_hash)
            return MetadataCheck("MATCH", problem=problem)

        problem, anchored = await self._archived(document.id, anchored_hash)
        if anchored is None:
            return MetadataCheck("UNAVAILABLE", problem=problem)

        changes = [
            RecordChange(field=name, anchored=value, current=live.get(name))
            for name, value in sorted(anchored.items())
            if name != "version"
            and value != live.get(name)
            and not await self._user_deleted(name, value, live)
        ]
        if not changes:
            return MetadataCheck("MATCH", anchored=anchored)
        log.error(
            "records.metadata.altered",
            document_id=document_id,
            fields=[c.field for c in changes],
        )
        return MetadataCheck("ALTERED", changes=changes, anchored=anchored)

    async def _user_deleted(self, name: str, anchored: Any, live: dict[str, Any]) -> bool:
        """`finalized_by` is SET NULL with its user. Cleared any other way, it's
        an edit: an attacker must not be able to clear it to hide a change."""
        if name != "finalized_by" or live[name] is not None or anchored is None:
            return False
        return await self.session.get(User, uuid.UUID(anchored)) is None

    async def _archived(
        self, document_id: uuid.UUID, anchored_hash: str
    ) -> tuple[MetadataProblem | None, dict[str, Any] | None]:
        """The archived record, only if it is exactly what was anchored."""
        if self.backup is None:
            return "archive_missing", None
        key = metadata_key(document_id, anchored_hash)
        try:
            # Asked first so a lost archive reads as gone, not as an outage.
            if not await self.backup.exists(key):
                log.error("records.metadata.archive_missing", document_id=str(document_id))
                return "archive_missing", None
            data = await self.backup.fetch(key)
        except Exception as e:
            log.error(
                "records.metadata.archive_read_failed",
                document_id=str(document_id),
                error=str(e),
            )
            return "archive_read", None
        if sha256_hex(data) != anchored_hash:
            log.error("records.metadata.archive_mismatch", document_id=str(document_id))
            return "archive_mismatch", None
        return None, json.loads(data)

    async def archived_pdf(
        self, document_id: uuid.UUID, onchain_hash: str
    ) -> bytes | None:
        """The archived PDF if it can be read and is what was anchored."""
        if self.backup is None:
            return None
        try:
            data = await self.backup.fetch(archive_key(document_id, onchain_hash))
        except Exception as e:
            log.warning(
                "records.archive_unread", document_id=str(document_id), error=str(e)
            )
            return None
        return data if sha256_hex(data) == onchain_hash else None

    async def snapshot_changes(
        self,
        snapshot: DocumentSnapshot,
        onchain_hash: str,
        archived: bytes | None = None,
    ) -> list[RecordChange]:
        """How the snapshot row differs from what the chain and archive support.

        `archived` is the PDF already proven against the chain. Without it, the
        size can't be judged, and a NULL `backup_key` may just mean no archive.
        """
        changes: list[RecordChange] = []
        if snapshot.file_sha256 != onchain_hash:
            changes.append(
                RecordChange(
                    field="file_sha256", anchored=onchain_hash, current=snapshot.file_sha256
                )
            )
        copy = await self.session.get(SignedCopy, snapshot.signed_copy_id)
        if (
            copy is None
            or copy.document_id != snapshot.document_id
            or copy.sha256 != onchain_hash
        ):
            changes.append(
                RecordChange(
                    field="signed_copy_id", anchored=None, current=str(snapshot.signed_copy_id)
                )
            )
        key = archive_key(snapshot.document_id, onchain_hash)
        if snapshot.backup_key != key and (archived is not None or snapshot.backup_key):
            changes.append(
                RecordChange(field="backup_key", anchored=key, current=snapshot.backup_key)
            )
        if archived is not None and snapshot.size_bytes != len(archived):
            changes.append(
                RecordChange(
                    field="size_bytes", anchored=len(archived), current=snapshot.size_bytes
                )
            )
        return changes
