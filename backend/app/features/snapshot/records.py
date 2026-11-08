"""Checks a finalized document's database records against the chain.

The rule is the PDF's: the chain says what is genuine, and the archive holds the
bytes to get it back. The metadata record is frozen at finalize as canonical
JSON, its hash anchored under `{document_id}:metadata`, and the JSON archived
beside the PDF. Parties aren't in it: sharing is allowed after finalize.

The analysis record (approved text, chunks, insight) is frozen the same way under
`{document_id}:analysis`. It is kept apart so a metadata check stays small and
its findings name single fields. Embeddings aren't in it: floats don't serialize
stably, and they are rebuilt from the chunk text.

A mismatch is diffed against the archived JSON, and only after that JSON
re-hashes to the anchor. A chain or archive read failure is unknown, never a
finding.
"""

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from app.features.chunks.models import DocumentChunk
from app.features.documents.extraction.models import DocumentExtraction
from app.features.documents.models import Document, SignedCopy
from app.features.insights.models import DocumentInsight
from app.features.users.models import User
from app.infrastructure.backup import BackupProvider
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.observability.logging_setup import log
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import DocumentSnapshot
from .schemas import MetadataStatus, RecordChange

METADATA_RECORD_VERSION = 1
METADATA_ANCHOR_SUFFIX = ":metadata"
ANALYSIS_RECORD_VERSION = 1
ANALYSIS_ANCHOR_SUFFIX = ":analysis"
RECORD_ANCHOR_SUFFIXES = {
    METADATA_ANCHOR_SUFFIX: "metadata record",
    ANALYSIS_ANCHOR_SUFFIX: "analysis record",
}
# Long text in a finding is shown around its first difference, not whole.
EXCERPT_CHARS = 80

INSIGHT_FIELDS = (
    "confidence",
    "engine",
    "engine_version",
    "entities",
    "labels",
    "risk_flags",
    "summary",
)
CHUNK_FIELDS = ("engine", "engine_version", "strategy", "text")

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


def analysis_key(document_id: uuid.UUID, analysis_hash: str) -> str:
    return f"{document_id}/analysis-{analysis_hash}.json"


def analysis_anchor_id(document_id: uuid.UUID | str) -> str:
    return f"{document_id}{ANALYSIS_ANCHOR_SUFFIX}"


def record_anchor(anchored_id: str) -> tuple[str, str] | None:
    """`(document_id, record name)` for a record anchor; None for a PDF anchor."""
    for suffix, name in RECORD_ANCHOR_SUFFIXES.items():
        if anchored_id.endswith(suffix):
            return anchored_id.removesuffix(suffix), name
    return None


def is_record_anchor(anchored_id: str) -> bool:
    return record_anchor(anchored_id) is not None


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
    return {
        "book_id": str(document.book_id) if document.book_id else None,
        "doc_no": document.doc_no,
        "file_name": document.file_name,
        "finalized_at": _timestamp(document.finalized_at),
        "finalized_by": str(document.finalized_by) if document.finalized_by else None,
        "page_no": document.page_no,
        "user_id": str(document.user_id),
        "version": METADATA_RECORD_VERSION,
    }


def _timestamp(value: datetime | None) -> str | None:
    return (
        value.astimezone(timezone.utc).isoformat(timespec="microseconds")
        if value
        else None
    )


def analysis_record(
    extraction: DocumentExtraction | None,
    chunks: list[DocumentChunk],
    insight: DocumentInsight | None,
) -> dict[str, Any]:
    """What processing produced and the lawyer approved, frozen at finalize."""
    return {
        "approved_text": extraction.approved_text if extraction else None,
        "chunks": [
            {"chunk_index": c.chunk_index, **{f: getattr(c, f) for f in CHUNK_FIELDS}}
            for c in sorted(chunks, key=lambda c: c.chunk_index)
        ],
        "insight": (
            {f: getattr(insight, f) for f in INSIGHT_FIELDS} if insight else None
        ),
        "reviewed_at": _timestamp(extraction.reviewed_at) if extraction else None,
        "reviewed_by": (
            str(extraction.reviewed_by)
            if extraction and extraction.reviewed_by
            else None
        ),
        "version": ANALYSIS_RECORD_VERSION,
    }


@dataclass
class AnalysisRows:
    extraction: DocumentExtraction | None
    chunks: list[DocumentChunk]
    insight: DocumentInsight | None

    def record(self) -> dict[str, Any]:
        return analysis_record(self.extraction, self.chunks, self.insight)


async def analysis_rows(session: AsyncSession, document_id: uuid.UUID) -> AnalysisRows:
    """The live rows the analysis record is built from. The newest insight is
    the one shown, so it is the one judged."""
    extraction = (
        await session.execute(
            select(DocumentExtraction).where(
                DocumentExtraction.document_id == document_id
            )
        )
    ).scalar_one_or_none()
    chunks = (
        await session.execute(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)
        )
    ).scalars()
    insight = (
        await session.execute(
            select(DocumentInsight)
            .where(DocumentInsight.document_id == document_id)
            .order_by(DocumentInsight.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return AnalysisRows(extraction, list(chunks), insight)


def _shown(value: Any, other: Any = None) -> str | int | None:
    """A value as a finding shows it: short, and around where it differs."""
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    text = value if isinstance(value, str) else canonical_json(value).decode()
    if len(text) <= EXCERPT_CHARS:
        return text
    other_text = (
        other
        if isinstance(other, str) or other is None
        else canonical_json(other).decode()
    ) or ""
    start = next(
        (i for i, (a, b) in enumerate(zip(text, other_text)) if a != b),
        min(len(text), len(other_text)),
    )
    start = max(0, start - EXCERPT_CHARS // 4)
    end = start + EXCERPT_CHARS
    return f"{'…' if start else ''}{text[start:end]}{'…' if end < len(text) else ''}"


def _change(name: str, anchored: Any, current: Any) -> RecordChange:
    return RecordChange(
        field=name,
        anchored=_shown(anchored, current),
        current=_shown(current, anchored),
    )


def analysis_changes(
    anchored: dict[str, Any], live: dict[str, Any]
) -> list[RecordChange]:
    """Changes by part: `approved_text`, `chunks[3].text`, `insight.risk_flags`.
    A whole chunk or insight that is missing or extra is named on its own."""
    changes = [
        _change(name, anchored.get(name), live.get(name))
        for name in ("approved_text", "reviewed_at", "reviewed_by")
        if anchored.get(name) != live.get(name)
    ]
    anchored_chunks = {c["chunk_index"]: c for c in anchored.get("chunks") or []}
    live_chunks = {c["chunk_index"]: c for c in live.get("chunks") or []}
    for index in sorted(anchored_chunks.keys() | live_chunks.keys()):
        was, now = anchored_chunks.get(index), live_chunks.get(index)
        if was is None or now is None:
            changes.append(
                _change(
                    f"chunks[{index}]",
                    was and was["text"],
                    now and now["text"],
                )
            )
            continue
        changes += [
            _change(f"chunks[{index}].{f}", was.get(f), now.get(f))
            for f in CHUNK_FIELDS
            if was.get(f) != now.get(f)
        ]
    was_insight, now_insight = anchored.get("insight"), live.get("insight")
    if was_insight is None or now_insight is None:
        if was_insight != now_insight:
            changes.append(
                _change(
                    "insight",
                    was_insight and was_insight.get("summary"),
                    now_insight and now_insight.get("summary"),
                )
            )
    else:
        changes += [
            _change(f"insight.{f}", was_insight.get(f), now_insight.get(f))
            for f in INSIGHT_FIELDS
            if was_insight.get(f) != now_insight.get(f)
        ]
    return changes


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
        live = metadata_record(document)
        check = await self._check(
            "metadata",
            document.id,
            metadata_anchor_id(document.id),
            metadata_key,
            live,
            check_archive,
        )
        if check.anchored is None or check.status != "ALTERED":
            return check
        check.changes = [
            RecordChange(field=name, anchored=value, current=live.get(name))
            for name, value in sorted(check.anchored.items())
            if name != "version"
            and value != live.get(name)
            and not await self._user_deleted(name, value, live)
        ]
        return self._judged("metadata", document.id, check)

    async def analysis(
        self, document: Document, *, check_archive: bool = False
    ) -> MetadataCheck:
        """The same check for the approved text, chunks and insight."""
        live = (await analysis_rows(self.session, document.id)).record()
        check = await self._check(
            "analysis",
            document.id,
            analysis_anchor_id(document.id),
            analysis_key,
            live,
            check_archive,
        )
        if check.anchored is None or check.status != "ALTERED":
            return check
        check.changes = [
            change
            for change in analysis_changes(check.anchored, live)
            if not (
                change.field == "reviewed_by"
                and await self._user_deleted(
                    "reviewed_by", check.anchored.get("reviewed_by"), live
                )
            )
        ]
        return self._judged("analysis", document.id, check)

    async def _check(
        self,
        name: str,
        document_id: uuid.UUID,
        anchor_id: str,
        key: Callable[[uuid.UUID, str], str],
        live: dict[str, Any],
        check_archive: bool,
    ) -> MetadataCheck:
        """MATCH, or ALTERED with the proven archived record for the caller to diff."""
        try:
            anchor = await self.chain.get_document_hash(
                document_id=anchor_id, retry_empty=False
            )
        except Exception as e:
            log.error(
                f"records.{name}.chain_read_failed",
                document_id=str(document_id),
                error=str(e),
            )
            return MetadataCheck("UNAVAILABLE", problem="chain")
        if anchor is None:
            return MetadataCheck("NOT_ANCHORED")

        anchored_hash = normalise_hash(anchor.data_hash)
        if sha256_hex(canonical_json(live)) == anchored_hash:
            if not check_archive:
                return MetadataCheck("MATCH")
            problem, _ = await self._archived(
                name, key(document_id, anchored_hash), document_id, anchored_hash
            )
            return MetadataCheck("MATCH", problem=problem)

        problem, anchored = await self._archived(
            name, key(document_id, anchored_hash), document_id, anchored_hash
        )
        if anchored is None:
            return MetadataCheck("UNAVAILABLE", problem=problem)
        return MetadataCheck("ALTERED", anchored=anchored)

    @staticmethod
    def _judged(
        name: str, document_id: uuid.UUID, check: MetadataCheck
    ) -> MetadataCheck:
        if not check.changes:
            return MetadataCheck("MATCH", anchored=check.anchored)
        log.error(
            f"records.{name}.altered",
            document_id=str(document_id),
            fields=[c.field for c in check.changes],
        )
        return check

    async def _user_deleted(
        self, name: str, anchored: Any, live: dict[str, Any]
    ) -> bool:
        """`finalized_by` and `reviewed_by` are SET NULL with their user. Cleared
        any other way, it's an edit: an attacker must not be able to clear one to
        hide a change."""
        if (
            name not in ("finalized_by", "reviewed_by")
            or live[name] is not None
            or anchored is None
        ):
            return False
        return await self.session.get(User, uuid.UUID(anchored)) is None

    async def _archived(
        self, name: str, key: str, document_id: uuid.UUID, anchored_hash: str
    ) -> tuple[MetadataProblem | None, dict[str, Any] | None]:
        """The archived record, only if it is exactly what was anchored."""
        if self.backup is None:
            return "archive_missing", None
        try:
            # Asked first so a lost archive reads as gone, not as an outage.
            if not await self.backup.exists(key):
                log.error(
                    f"records.{name}.archive_missing", document_id=str(document_id)
                )
                return "archive_missing", None
            data = await self.backup.fetch(key)
        except Exception as e:
            log.error(
                f"records.{name}.archive_read_failed",
                document_id=str(document_id),
                error=str(e),
            )
            return "archive_read", None
        if sha256_hex(data) != anchored_hash:
            log.error(f"records.{name}.archive_mismatch", document_id=str(document_id))
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
