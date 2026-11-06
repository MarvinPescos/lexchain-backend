"""One-off move of existing documents onto the current contract.

The old contract has no write-once or recorder checks, so nothing it holds can
be trusted on its own. A document is re-anchored only when its live PDF, its
snapshot and the old contract's hash all agree; the rest are listed for a person
to review. Each one re-anchored also gets its metadata record, which is built
from the row as it stands now: nothing earlier exists to check it against.

Dry run by default:

    python -m app.workers.reanchor_documents --old-contract 0x... [--apply]
"""

import argparse
import asyncio
from dataclasses import dataclass, field

from app.features.blockchain.repository import OnChainRecordRepository
from app.features.documents.constants import LIFECYCLE_FINALIZED
from app.features.documents.models import Document, SignedCopy
from app.features.snapshot.backups import SnapshotArchive
from app.features.snapshot.models import DocumentSnapshot
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.features.snapshot.records import (
    canonical_json,
    metadata_anchor_id,
    metadata_record,
    normalise_hash,
    sha256_hex,
)
from app.infrastructure.backup import BackupProvider
from app.infrastructure.blockchain import BlockchainProvider
from app.infrastructure.clients.supabase_storage import get_storage_client
from app.infrastructure.observability.logging_setup import log
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class ReanchorReport:
    applied: bool
    ready: list[str] = field(default_factory=list)
    done: list[str] = field(default_factory=list)
    review: dict[str, str] = field(default_factory=dict)


async def _review_reason(
    session: AsyncSession,
    snapshot: DocumentSnapshot,
    old_chain: BlockchainProvider,
) -> str | None:
    """Why this document can't be re-anchored, or None if every hash agrees."""
    document = await session.get(Document, snapshot.document_id)
    assert document is not None  # the snapshot's foreign key
    if document.lifecycle != LIFECYCLE_FINALIZED:
        return f"lifecycle is {document.lifecycle}"
    record = await old_chain.get_document_hash(
        str(snapshot.document_id), retry_empty=False
    )
    if record is None:
        return "no anchor on the old contract"
    if normalise_hash(record.data_hash) != snapshot.file_sha256:
        return "the old contract's hash differs from the snapshot"
    copy = (
        await session.get(SignedCopy, document.signed_copy_id)
        if document.signed_copy_id
        else None
    )
    if copy is None:
        return "no signed copy record"
    try:
        content = await get_storage_client().download(copy.storage_url)
    except FileNotFoundError:
        return "the live file is missing"
    if sha256_hex(content) != snapshot.file_sha256:
        return "the live file differs from the snapshot"
    return None


async def _apply(
    session: AsyncSession,
    snapshot: DocumentSnapshot,
    new_chain: BlockchainProvider,
    backup: BackupProvider,
) -> None:
    document = await session.get(Document, snapshot.document_id)
    assert document is not None  # the snapshot's foreign key
    document_id = str(document.id)

    metadata = canonical_json(metadata_record(document))
    if not await SnapshotArchive(session, backup).archive_metadata(snapshot, metadata):
        raise RuntimeError("the metadata record could not be archived")

    record = await new_chain.store_document_hash(document_id, snapshot.file_sha256)
    anchored = await OnChainRecordRepository(session).get_by_document_id(document.id)
    if anchored is not None:
        anchored.tx_hash = record.tx_hash
        anchored.data_hash = record.data_hash
        anchored.onchain_timestamp = record.timestamp
        anchored.issued_by = record.issued_by
    await session.commit()

    await new_chain.store_document_hash(metadata_anchor_id(document.id), sha256_hex(metadata))


async def reanchor(
    session: AsyncSession,
    old_chain: BlockchainProvider,
    new_chain: BlockchainProvider,
    backup: BackupProvider,
    *,
    apply: bool = False,
) -> ReanchorReport:
    """One document's failure lands it in the review list, not the whole run."""
    result = await session.execute(
        select(DocumentSnapshot.document_id).order_by(DocumentSnapshot.created_at)
    )
    repo = DocumentSnaphotRepository(session)
    report = ReanchorReport(applied=apply)
    # IDs, not rows: a rollback expires every row loaded so far.
    for snapshot_document_id in result.scalars().all():
        document_id = str(snapshot_document_id)
        try:
            snapshot = await repo.get_by_document_id(snapshot_document_id)
            assert snapshot is not None
            if await new_chain.get_document_hash(document_id, retry_empty=False):
                report.review[document_id] = "already anchored on the current contract"
                continue
            reason = await _review_reason(session, snapshot, old_chain)
            if reason is not None:
                report.review[document_id] = reason
            elif apply:
                await _apply(session, snapshot, new_chain, backup)
                report.done.append(document_id)
            else:
                report.ready.append(document_id)
        except Exception as e:
            await session.rollback()
            log.error("reanchor.failed", document_id=document_id, error=str(e))
            report.review[document_id] = f"failed: {type(e).__name__}: {e}"
    log.info(
        "reanchor.done",
        applied=apply,
        ready=len(report.ready),
        done=len(report.done),
        review=len(report.review),
    )
    return report


async def _main() -> None:
    import app.all_models  # noqa: F401 — registers all SQLAlchemy models
    from app.features.blockchain.dependencies import (
        get_blockchain_provider,
        get_provider_for_contract,
    )
    from app.infrastructure.backup import build_backup
    from app.infrastructure.database import AsyncSessionLocal

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-contract", required=True)
    parser.add_argument("--apply", action="store_true", help="Write to the chain")
    args = parser.parse_args()

    async with AsyncSessionLocal() as session:
        report = await reanchor(
            session,
            get_provider_for_contract(args.old_contract),
            get_blockchain_provider(),
            build_backup(),
            apply=args.apply,
        )
    print(f"{'Re-anchored' if args.apply else 'Would re-anchor'}:")
    for document_id in report.done or report.ready:
        print(f"  {document_id}")
    print("Needs review:")
    for document_id, reason in report.review.items():
        print(f"  {document_id}: {reason}")


if __name__ == "__main__":
    asyncio.run(_main())
