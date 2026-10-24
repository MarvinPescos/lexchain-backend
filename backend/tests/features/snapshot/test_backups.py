"""Archiving at finalize, and verification against the anchored hash."""

import hashlib

import pytest

from app.features.documents.workflow.finalization import DocumentFinalization
from app.features.snapshot.backups import SnapshotArchive
from app.features.snapshot.repository import DocumentSnaphotRepository
from tests.conftest import as_auth, pdf


class FakeBackup:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.down = False

    async def store(self, key, data, content_type):
        if self.down:
            raise ConnectionError("backup bucket unreachable")
        self.objects[key] = data

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
    await DocumentFinalization(session, queue, chain, backup).finalize(me, doc.document_id)
    return await DocumentSnaphotRepository(session).get_by_document_id(doc.document_id)


async def test_finalize_archives_the_frozen_copy(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)

    assert snapshot.backup_key == f"{snapshot.document_id}/{snapshot.file_sha256}.pdf"
    assert snapshot.backed_up_at is not None
    data = backup.objects[snapshot.backup_key]
    assert hashlib.sha256(data).hexdigest() == snapshot.file_sha256


async def test_backup_outage_does_not_block_finalize(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    backup.down = True
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)

    assert snapshot is not None
    assert snapshot.backup_key is None


async def test_verify_reports_ok_missing_and_mismatch(
    session, intake, storage, queue, chain, backup, lawyer, open_book
):
    good = await _finalize(session, intake, queue, chain, backup, lawyer, open_book, "a")
    bad = await _finalize(session, intake, queue, chain, backup, lawyer, open_book, "b")
    gone = await _finalize(session, intake, queue, chain, backup, lawyer, open_book, "c")
    backup.objects[bad.backup_key] = b"tampered"
    del backup.objects[gone.backup_key]

    report = await SnapshotArchive(session, backup).verify()

    assert (report.checked, report.ok, report.mismatch, report.missing) == (3, 1, 1, 1)
    statuses = {p.document_id: p.status for p in report.problems}
    assert statuses == {bad.document_id: "mismatch", gone.document_id: "missing"}
    assert good.document_id not in statuses


async def test_repair_rearchives_from_the_live_copy(
    session, intake, live_storage, queue, chain, backup, lawyer, open_book
):
    backup.down = True
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)
    backup.down = False

    report = await SnapshotArchive(session, backup).verify(repair=True)

    assert (report.repaired, report.ok) == (1, 0)
    assert snapshot.backup_key in backup.objects
    assert (await SnapshotArchive(session, backup).verify()).ok == 1


async def test_repair_refuses_a_live_copy_that_changed(
    session, intake, live_storage, queue, chain, backup, lawyer, open_book
):
    backup.down = True
    snapshot = await _finalize(session, intake, queue, chain, backup, lawyer, open_book)
    backup.down = False
    for url in live_storage.objects:
        live_storage.objects[url] = b"altered after finalize"

    report = await SnapshotArchive(session, backup).verify(repair=True)

    assert report.unrecoverable == 1
    assert snapshot.backup_key is None
    assert backup.objects == {}
