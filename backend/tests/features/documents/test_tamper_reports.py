"""Tamper reports: the open problems of a lawyer's documents, and restoring them."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.features.blockchain.models import VerificationResult
from app.features.blockchain.service import BlockchainService
from app.features.documents.models import Document, SignedCopy
from app.features.documents.workflow.finalization import DocumentFinalization
from app.features.documents.workflow.restoration import DocumentRestoration
from app.features.documents.workflow.tamper_reports import TamperReports
from app.features.notifications.models import Notification, NotificationType
from app.features.snapshot.backups import SnapshotArchive
from app.features.snapshot.repository import DocumentSnaphotRepository
from tests.conftest import as_auth, mark_processed, pdf
from tests.features.blockchain.test_tamper_report import FakeBackup


@pytest.fixture
def backup() -> FakeBackup:
    return FakeBackup()


@pytest.fixture
def reports(session, storage, chain, backup) -> TamperReports:
    return TamperReports(DocumentRestoration(session, chain, backup))


async def finalize(session, intake, chain, backup, user, book, name="Deed"):
    me = as_auth(user)
    doc = await intake.upload_signed(me, pdf(name), name, book.id)
    await mark_processed(session, doc.document_id)
    await DocumentFinalization(session, chain, backup).finalize(me, doc.document_id)
    return doc.document_id


@pytest.fixture
async def document_id(session, intake, queue, chain, backup, storage, lawyer, open_book):
    return await finalize(session, intake, chain, backup, lawyer, open_book)


async def flag(session, document_id, status="RECORD_ALTERED"):
    """What a check leaves behind when it finds something."""
    session.add(
        VerificationResult(
            document_id=document_id,
            verified_at=datetime.now(timezone.utc),
            status=status,
            is_authentic=status == "RECORD_ALTERED",
            baseline_trusted=True,
            tamper_report_status="NOT_APPLICABLE",
        )
    )
    await session.commit()


def url_of(storage, document_id):
    [url] = [u for u in storage.objects if str(document_id) in u]
    return url


async def test_nothing_flagged_lists_nothing(reports, document_id, lawyer):
    assert (await reports.list_open(as_auth(lawyer))).total == 0


async def test_edited_file_is_listed_with_both_hashes(
    session, reports, document_id, storage, lawyer
):
    storage.objects[url_of(storage, document_id)] += b" tampered"
    await flag(session, document_id, "TAMPERED")

    [report] = (await reports.list_open(as_auth(lawyer))).reports

    assert report.document_id == document_id
    assert report.status == "TAMPERED"
    assert report.restorable is True
    [change] = report.changes
    assert change.kind == "file"
    assert change.genuine != change.current and change.current is not None


async def test_deleted_file_has_no_current_hash(
    session, reports, document_id, storage, lawyer
):
    del storage.objects[url_of(storage, document_id)]
    await flag(session, document_id, "FILE_MISSING")

    [report] = (await reports.list_open(as_auth(lawyer))).reports

    assert report.status == "FILE_MISSING"
    [change] = report.changes
    assert (change.kind, change.current) == ("file", None)


async def test_edited_metadata_names_the_field_on_a_genuine_pdf(
    session, reports, document_id, lawyer
):
    document = await session.get(Document, document_id)
    genuine = document.file_name
    document.file_name = "Forged"
    await session.commit()
    await flag(session, document_id)

    [report] = (await reports.list_open(as_auth(lawyer))).reports

    assert report.status == "AUTHENTIC"
    [change] = report.changes
    assert (change.kind, change.field) == ("metadata", "file_name")
    assert (change.genuine, change.current) == (genuine, "Forged")


async def test_edited_snapshot_and_lifecycle_are_listed(
    session, reports, document_id, lawyer
):
    snapshot = await DocumentSnaphotRepository(session).get_by_document_id(document_id)
    snapshot.file_sha256 = "ab" * 32
    document = await session.get(Document, document_id)
    document.lifecycle = "SIGNED"
    await session.commit()
    await flag(session, document_id)

    [report] = (await reports.list_open(as_auth(lawyer))).reports

    kinds = {(c.kind, c.field) for c in report.changes}
    assert ("snapshot", "file_sha256") in kinds
    assert ("lifecycle", "lifecycle") in kinds


async def test_a_problem_already_fixed_is_not_listed(
    session, reports, document_id, lawyer
):
    await flag(session, document_id)

    assert (await reports.list_open(as_auth(lawyer))).total == 0


async def test_a_lost_archive_blocks_restore(
    session, reports, document_id, storage, backup, lawyer
):
    storage.objects[url_of(storage, document_id)] += b" tampered"
    backup.objects.clear()
    await flag(session, document_id, "TAMPERED")

    [report] = (await reports.list_open(as_auth(lawyer))).reports

    assert report.restorable is False
    assert report.blocked_reason == "archive_missing"


async def test_a_document_with_no_anchor_is_not_listed(
    session, reports, document_id, chain, lawyer
):
    chain.records.clear()
    await flag(session, document_id, "TAMPERED")

    assert (await reports.list_open(as_auth(lawyer))).total == 0


async def test_only_the_owners_documents_are_listed(
    session, reports, document_id, storage, lawyer, other_lawyer
):
    storage.objects[url_of(storage, document_id)] += b" tampered"
    await flag(session, document_id, "TAMPERED")

    assert (await reports.list_open(as_auth(other_lawyer))).total == 0


async def test_restore_all_fixes_every_kind_at_once(
    session, intake, reports, document_id, chain, backup, storage, lawyer, open_book
):
    second = await finalize(session, intake, chain, backup, lawyer, open_book, "Lease")
    storage.objects[url_of(storage, document_id)] += b" tampered"
    await flag(session, document_id, "TAMPERED")
    document = await session.get(Document, second)
    document.file_name = "Forged"
    await session.commit()
    await flag(session, second)

    result = await reports.restore_all(as_auth(lawyer))

    assert {r.document_id for r in result.restored} == {document_id, second}
    assert result.failed == []
    assert (await reports.list_open(as_auth(lawyer))).total == 0
    verifier = BlockchainService(session, chain, backup)
    for restored in (document_id, second):
        after = await verifier.verify_document(as_auth(lawyer), restored)
        assert after.status == "AUTHENTIC" and after.metadata_status == "MATCH"


async def test_restore_all_carries_on_past_a_failure(
    session, intake, reports, document_id, chain, backup, storage, lawyer, open_book
):
    second = await finalize(session, intake, chain, backup, lawyer, open_book, "Lease")
    storage.objects[url_of(storage, document_id)] += b" tampered"
    await flag(session, document_id, "TAMPERED")
    for key in [k for k in backup.objects if str(document_id) in k]:
        del backup.objects[key]
    document = await session.get(Document, second)
    document.file_name = "Forged"
    await session.commit()
    await flag(session, second)

    result = await reports.restore_all(as_auth(lawyer))

    assert [r.document_id for r in result.restored] == [second]
    [failure] = result.failed
    assert failure.document_id == document_id


async def test_a_new_finding_after_a_restore_is_listed_again(
    session, reports, document_id, storage, lawyer
):
    url = url_of(storage, document_id)
    storage.objects[url] += b" tampered"
    await flag(session, document_id, "TAMPERED")
    await reports.restore_all(as_auth(lawyer))

    live = [u for u in storage.objects if str(document_id) in u]
    document = await session.get(Document, document_id)
    document.file_name = "Forged"
    await session.commit()
    await flag(session, document_id)

    assert live and (await reports.list_open(as_auth(lawyer))).total == 1


async def alerts(session, type) -> int:
    return await session.scalar(
        select(func.count()).select_from(Notification).where(Notification.type == type)
    )


async def test_the_same_edit_after_a_restore_is_a_new_finding(
    session, reports, document_id, chain, backup, lawyer
):
    archive = SnapshotArchive(session, backup, chain)
    document = await session.get(Document, document_id)
    document.file_name = "Forged"
    await session.commit()
    await archive.verify()
    await reports.restore_all(as_auth(lawyer))

    document = await session.get(Document, document_id)
    document.file_name = "Forged"
    await session.commit()
    await archive.verify()

    assert await alerts(session, NotificationType.RECORD_ALTERED) == 2
    assert (await reports.list_open(as_auth(lawyer))).total == 1


async def test_the_same_file_after_a_restore_is_a_new_finding(
    session, reports, document_id, chain, backup, storage, lawyer
):
    archive = SnapshotArchive(session, backup, chain)
    url = url_of(storage, document_id)
    forged = storage.objects[url] + b" tampered"
    storage.objects[url] = forged
    await archive.verify()
    result = await reports.restore_all(as_auth(lawyer))
    [restored] = result.restored

    document = await session.get(Document, document_id)
    copy = await session.get(SignedCopy, document.signed_copy_id)
    storage.objects[copy.storage_url] = forged
    await archive.verify()

    assert await alerts(session, NotificationType.LIVE_FILE_TAMPERED) == 2
    assert (await reports.list_open(as_auth(lawyer))).total == 1


async def test_a_repeat_check_without_a_restore_still_alerts_once(
    session, document_id, chain, backup
):
    archive = SnapshotArchive(session, backup, chain)
    document = await session.get(Document, document_id)
    document.file_name = "Forged"
    await session.commit()

    await archive.verify()
    await archive.verify()

    assert await alerts(session, NotificationType.RECORD_ALTERED) == 1
