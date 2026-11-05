"""Archiving at finalize, and verification against the anchored hash."""

import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.features.blockchain.models import VerificationResult
from app.features.blockchain.service import BlockchainService
from app.features.documents.models import Document
from app.features.documents.workflow.finalization import DocumentFinalization
from app.features.documents.workflow.restoration import DocumentRestoration
from app.features.snapshot.backups import SnapshotArchive
from app.features.notifications.models import Notification, NotificationType
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.shared.errors.exceptions import ServiceUnavailableError
from tests.conftest import as_auth, pdf, mark_processed


class FakeBackup:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.down = False
        # Simulates a write that lands but does not read back intact.
        self.corrupt = False

    async def store(self, key, data, content_type):
        if self.down:
            raise ConnectionError("backup bucket unreachable")
        if key in self.objects:
            raise FileExistsError(key)  # write-once, like an object-locked bucket
        self.objects[key] = data + b"x" if self.corrupt else data

    async def fetch(self, key):
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]

    async def exists(self, key):
        return key in self.objects


@pytest.fixture
def backup() -> FakeBackup:
    return FakeBackup()


@pytest.fixture
def live_storage(monkeypatch, storage):
    monkeypatch.setattr("app.features.snapshot.backups.get_storage_client", lambda: storage)
    return storage


async def _finalize(session, intake, queue, chain, backup, lawyer, book, body="deed"):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf(body), "Deed", book.id)
    await mark_processed(session, doc.document_id)
    await DocumentFinalization(session, chain, backup).finalize(me, doc.document_id)
    return await DocumentSnaphotRepository(session).get_by_document_id(doc.document_id)


async def test_finalize_archives_the_frozen_copy(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)

    assert snapshot.backup_key == f"{snapshot.document_id}/{snapshot.file_sha256}.pdf"
    assert snapshot.backed_up_at is not None
    data = backup.objects[snapshot.backup_key]
    assert hashlib.sha256(data).hexdigest() == snapshot.file_sha256


async def _legacy(session, intake, queue, chain, lawyer, book):
    """A document finalized before archiving was guaranteed: no archive."""
    return await _finalize(session, intake, queue, chain, None, lawyer, book)


@pytest.mark.parametrize("failure", ["down", "corrupt"])
async def test_archive_failure_blocks_finalize(
    failure, session, intake, storage, queue, chain, backup, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await session.commit()
    setattr(backup, failure, True)

    with pytest.raises(ServiceUnavailableError):
        await DocumentFinalization(session, chain, backup).finalize(
            me, doc.document_id
        )
    await session.rollback()

    assert chain.records == {}
    assert await DocumentSnaphotRepository(session).get_by_document_id(doc.document_id) is None
    document = await session.get(Document, doc.document_id)
    assert document.finalized_at is None


async def test_finalize_can_be_retried_after_an_archive_outage(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await session.commit()
    backup.down = True
    with pytest.raises(ServiceUnavailableError):
        await DocumentFinalization(session, chain, backup).finalize(
            me, doc.document_id
        )
    await session.rollback()

    backup.down = False
    await DocumentFinalization(session, chain, backup).finalize(me, doc.document_id)

    snapshot = await DocumentSnaphotRepository(session).get_by_document_id(doc.document_id)
    assert hashlib.sha256(backup.objects[snapshot.backup_key]).hexdigest() == snapshot.file_sha256
    assert len(chain.records) == 1


async def test_retry_after_a_failed_anchor_reuses_the_archive(
    session, intake, storage, queue, chain, backup, lawyer, open_book, monkeypatch
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await session.commit()

    async def chain_down(*a, **k):
        raise ConnectionError("rpc unreachable")

    with monkeypatch.context() as m:
        m.setattr(chain, "store_document_hash", chain_down)
        with pytest.raises(ConnectionError):
            await DocumentFinalization(session, chain, backup).finalize(
                me, doc.document_id
            )
    await session.rollback()
    archived = dict(backup.objects)

    await DocumentFinalization(session, chain, backup).finalize(me, doc.document_id)

    snapshot = await DocumentSnaphotRepository(session).get_by_document_id(doc.document_id)
    assert snapshot.backup_key in archived
    assert backup.objects == archived


async def test_finalize_never_overwrites_a_bad_existing_archive(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await session.commit()
    backup.corrupt = True
    with pytest.raises(ServiceUnavailableError):
        await DocumentFinalization(session, chain, backup).finalize(
            me, doc.document_id
        )
    await session.rollback()
    bad = dict(backup.objects)

    backup.corrupt = False
    with pytest.raises(ServiceUnavailableError):
        await DocumentFinalization(session, chain, backup).finalize(
            me, doc.document_id
        )
    await session.rollback()

    assert backup.objects == bad
    assert chain.records == {}


@pytest.mark.parametrize("change", ["alter", "delete"])
async def test_archive_survives_changes_to_the_active_copy(
    change, session, intake, live_storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)
    archived = backup.objects[snapshot.backup_key]
    for url in list(live_storage.objects):
        if change == "alter":
            live_storage.objects[url] = b"altered after finalize"
        else:
            del live_storage.objects[url]

    report = await SnapshotArchive(session, backup, chain).verify(repair=True)

    assert report.ok == 1
    assert backup.objects[snapshot.backup_key] == archived


async def test_verify_reports_ok_missing_and_mismatch(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    good = await _finalize(session, intake, queue, chain, backup, lawyer, open_book, "a")
    bad = await _finalize(session, intake, queue, chain, backup, lawyer, open_book, "b")
    gone = await _finalize(session, intake, queue, chain, backup, lawyer, open_book, "c")
    backup.objects[bad.backup_key] = b"tampered"
    del backup.objects[gone.backup_key]

    report = await SnapshotArchive(session, backup, chain).verify()

    assert (report.checked, report.ok, report.mismatch, report.missing) == (3, 1, 1, 1)
    statuses = {p.document_id: p.status for p in report.problems}
    assert statuses == {bad.document_id: "mismatch", gone.document_id: "missing"}
    assert good.document_id not in statuses


async def test_verify_reports_an_archive_of_the_wrong_size(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)
    snapshot.size_bytes += 1

    report = await SnapshotArchive(session, backup).verify()

    assert report.mismatch == 1


async def test_verify_catches_an_archive_and_snapshot_forged_to_agree(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)
    forged = b"forged"
    backup.objects[snapshot.backup_key] = forged
    snapshot.file_sha256 = hashlib.sha256(forged).hexdigest()
    snapshot.size_bytes = len(forged)

    report = await SnapshotArchive(session, backup, chain).verify()

    assert report.mismatch == 1


@pytest.mark.parametrize("repair", [False, True])
async def test_chain_outage_leaves_archives_unverified(
    repair, session, intake, live_storage, queue, chain, backup, lawyer, open_book, monkeypatch
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)
    stored = dict(backup.objects)

    async def chain_down(*a, **k):
        raise ConnectionError("rpc unreachable")

    monkeypatch.setattr(chain, "get_document_hash", chain_down)

    report = await SnapshotArchive(session, backup, chain).verify(repair=repair)

    assert (report.ok, report.unverified, report.unrecoverable) == (0, 1, 0)
    assert report.problems[0].document_id == snapshot.document_id
    assert backup.objects == stored


async def test_verify_without_a_chain_reader_confirms_nothing(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    await _finalize(session, intake, queue, chain, backup, lawyer, open_book)

    report = await SnapshotArchive(session, backup).verify()

    assert (report.ok, report.unverified) == (0, 1)


async def test_repair_rearchives_a_legacy_document_from_the_live_copy(
    session, intake, live_storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _legacy(session, intake, queue, chain, lawyer, open_book)

    report = await SnapshotArchive(session, backup, chain).verify(repair=True)

    assert (report.repaired, report.ok) == (1, 0)
    assert snapshot.backup_key in backup.objects
    assert (await SnapshotArchive(session, backup, chain).verify()).ok == 1


async def test_repair_refuses_a_live_copy_that_changed(
    session, intake, live_storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _legacy(session, intake, queue, chain, lawyer, open_book)
    for url in live_storage.objects:
        live_storage.objects[url] = b"altered after finalize"

    report = await SnapshotArchive(session, backup, chain).verify(repair=True)

    assert report.unrecoverable == 1
    assert snapshot.backup_key is None
    assert backup.objects == {}


async def test_repair_does_not_trust_an_archive_that_reads_back_wrong(
    session, intake, live_storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _legacy(session, intake, queue, chain, lawyer, open_book)
    backup.corrupt = True

    report = await SnapshotArchive(session, backup, chain).verify(repair=True)

    assert report.unrecoverable == 1
    assert snapshot.backup_key is None


async def test_repair_requires_the_snapshot_to_match_the_chain(
    session, intake, live_storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _legacy(session, intake, queue, chain, lawyer, open_book)
    # The snapshot row and the live copy were both rewritten to agree with each
    # other — but not with the chain.
    forged = b"forged"
    for url in live_storage.objects:
        live_storage.objects[url] = forged
    snapshot.file_sha256 = hashlib.sha256(forged).hexdigest()
    snapshot.size_bytes = len(forged)

    report = await SnapshotArchive(session, backup, chain).verify(repair=True)

    assert report.unrecoverable == 1
    assert backup.objects == {}


async def test_repair_without_a_chain_reader_fails_closed(
    session, intake, live_storage, queue, chain, backup, lawyer, open_book
):
    await _legacy(session, intake, queue, chain, lawyer, open_book)

    report = await SnapshotArchive(session, backup).verify(repair=True)

    assert report.unrecoverable == 1
    assert backup.objects == {}


# === Live file check ===


async def _findings(session, document_id) -> list[VerificationResult]:
    result = await session.execute(
        select(VerificationResult)
        .where(VerificationResult.document_id == document_id)
        .order_by(VerificationResult.verified_at)
    )
    return list(result.scalars())


async def _alerts(session, user_id) -> list[Notification]:
    result = await session.execute(
        select(Notification).where(
            Notification.user_id == user_id,
            Notification.type == NotificationType.LIVE_FILE_TAMPERED,
        )
    )
    return list(result.scalars())


async def test_live_check_flags_a_changed_file_while_the_archive_is_ok(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)
    [url] = storage.objects
    storage.objects[url] = b"altered after finalize"

    report = await SnapshotArchive(session, backup, chain).verify()

    assert report.ok == 1
    assert (report.live.checked, report.live.ok, report.live.tampered) == (1, 0, 1)
    assert report.live.problems[0].document_id == snapshot.document_id
    assert report.live.problems[0].status == "TAMPERED"
    [row] = await _findings(session, snapshot.document_id)
    assert row.status == "TAMPERED"
    assert row.verified_by is None
    assert row.is_authentic is False
    assert row.baseline_trusted is True
    assert row.current_hash == hashlib.sha256(b"altered after finalize").hexdigest()
    assert row.onchain_hash == snapshot.file_sha256


async def test_live_check_runs_when_the_archive_is_bad(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)
    backup.objects[snapshot.backup_key] = b"bad archive"
    [url] = storage.objects
    storage.objects[url] = b"altered after finalize"

    report = await SnapshotArchive(session, backup, chain).verify()

    assert (report.mismatch, report.live.tampered) == (1, 1)


async def test_live_check_passes_an_untouched_file_and_records_nothing(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)

    report = await SnapshotArchive(session, backup, chain).verify()

    assert (report.live.checked, report.live.ok) == (1, 1)
    assert report.live.problems == []
    assert await _findings(session, snapshot.document_id) == []
    assert await _alerts(session, lawyer.id) == []


async def test_live_check_reads_the_restored_copy_not_the_snapshots(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)
    me = as_auth(lawyer)
    [url] = storage.objects
    storage.objects[url] = b"altered after finalize"
    await DocumentRestoration(session, chain, backup).restore(me, snapshot.document_id)
    document = await session.get(Document, snapshot.document_id)
    assert document.signed_copy_id != snapshot.signed_copy_id

    report = await SnapshotArchive(session, backup, chain).verify()

    assert (report.live.ok, report.live.tampered) == (1, 0)
    assert await _findings(session, snapshot.document_id) == []


async def test_live_check_reports_a_deleted_file_as_a_tamper_finding(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)
    storage.objects.clear()

    report = await SnapshotArchive(session, backup, chain).verify()

    assert report.live.file_missing == 1
    [row] = await _findings(session, snapshot.document_id)
    assert row.status == "FILE_MISSING"
    assert row.current_hash is None
    assert len(await _alerts(session, lawyer.id)) == 1


async def test_live_check_compares_with_the_chain_not_the_snapshot_row(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)
    [url] = storage.objects
    forged = b"forged"
    storage.objects[url] = forged
    snapshot.file_sha256 = hashlib.sha256(forged).hexdigest()
    await session.flush()

    report = await SnapshotArchive(session, backup, chain).verify()

    assert report.live.snapshot_compromised == 1
    [row] = await _findings(session, snapshot.document_id)
    assert row.status == "SNAPSHOT_COMPROMISED"
    assert row.baseline_trusted is False


@pytest.mark.parametrize("outage", ["storage", "chain"])
async def test_live_check_outage_is_unknown_with_no_row_or_alert(
    outage, session, intake, storage, queue, chain, backup, lawyer, open_book, monkeypatch
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)

    async def down(*a, **k):
        raise ConnectionError("unreachable")

    if outage == "storage":
        monkeypatch.setattr(storage, "download", down)
    else:
        monkeypatch.setattr(chain, "get_document_hash", down)

    report = await SnapshotArchive(session, backup, chain).verify()

    assert (report.live.unavailable, report.live.tampered) == (1, 0)
    assert report.live.problems[0].status == "VERIFICATION_UNAVAILABLE"
    assert await _findings(session, snapshot.document_id) == []
    assert await _alerts(session, lawyer.id) == []


async def test_a_tampered_file_alerts_once_and_a_new_hash_alerts_again(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)
    [url] = storage.objects
    storage.objects[url] = b"first edit"
    archive = SnapshotArchive(session, backup, chain)

    await archive.verify()
    await archive.verify()

    assert len(await _alerts(session, lawyer.id)) == 1
    assert len(await _findings(session, snapshot.document_id)) == 1

    storage.objects[url] = b"second edit"
    await archive.verify()

    assert len(await _alerts(session, lawyer.id)) == 2


async def test_opening_a_tampered_file_first_does_not_swallow_the_alert(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)
    [url] = storage.objects
    storage.objects[url] = b"altered after finalize"
    await BlockchainService(session, chain).verify_document(
        as_auth(lawyer), snapshot.document_id, tamper_report=False, record=True
    )

    await SnapshotArchive(session, backup, chain).verify()

    assert len(await _alerts(session, lawyer.id)) == 1


async def test_an_outage_row_does_not_swallow_the_alert_for_a_deleted_file(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)
    session.add(
        VerificationResult(
            document_id=snapshot.document_id,
            verified_at=datetime.now(timezone.utc),
            status="VERIFICATION_UNAVAILABLE",
            is_authentic=False,
            baseline_trusted=True,
            tamper_report_status="NOT_APPLICABLE",
        )
    )
    await session.flush()
    storage.objects.clear()

    await SnapshotArchive(session, backup, chain).verify()

    assert len(await _alerts(session, lawyer.id)) == 1
