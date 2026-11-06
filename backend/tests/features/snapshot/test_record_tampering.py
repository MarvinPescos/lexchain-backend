"""Detecting and restoring tampered records: metadata, snapshot, lifecycle, archive."""

import hashlib
import json

import pytest
from sqlalchemy import delete, select

from app.core import settings
from app.features.blockchain.models import VerificationResult
from app.features.blockchain.reconciliation import ChainReconciliation
from app.features.blockchain.report import IntegrityReport
from app.features.blockchain.service import BlockchainService
from app.features.documents.models import Document, DocumentParty
from app.features.documents.workflow.finalization import DocumentFinalization
from app.features.documents.workflow.restoration import DocumentRestoration
from app.features.notifications.models import Notification, NotificationType
from app.features.snapshot.backups import SnapshotArchive
from app.features.snapshot.records import canonical_json, metadata_record
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.features.users.models import User
from app.shared.errors.exceptions import ConflictError, ServiceUnavailableError
from tests.conftest import as_auth, mark_processed, pdf
from tests.features.documents.test_drafting import add_party
from tests.features.snapshot.test_backups import FakeBackup


@pytest.fixture
def backup() -> FakeBackup:
    return FakeBackup()


@pytest.fixture
def archive(session, storage, chain, backup) -> SnapshotArchive:
    return SnapshotArchive(session, backup, chain)


@pytest.fixture
def verifier(session, storage, chain, backup) -> BlockchainService:
    return BlockchainService(session, chain, backup)


@pytest.fixture
def restoration(session, storage, chain, backup) -> DocumentRestoration:
    return DocumentRestoration(session, chain, backup)


@pytest.fixture
def alert_emails(monkeypatch) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []

    async def record(subject, body):
        sent.append((subject, body))

    monkeypatch.setattr("app.features.snapshot.backups.send_backup_alert", record)
    return sent


@pytest.fixture
async def finalized(
    session, intake, queue, chain, backup, storage, lawyer, open_book
) -> Document:
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await DocumentFinalization(session, chain, backup).finalize(me, doc.document_id)
    return await session.get(Document, doc.document_id)


async def _snapshot(session, document):
    return await DocumentSnaphotRepository(session).get_by_document_id(document.id)


async def _notifications(session, type: NotificationType) -> list[Notification]:
    result = await session.execute(select(Notification).where(Notification.type == type))
    return list(result.scalars().all())


def _break_chain(monkeypatch, chain) -> None:
    async def down(*a, **k):
        raise ServiceUnavailableError("rpc unreachable")

    monkeypatch.setattr(chain, "get_document_hash", down)


def _metadata_objects(backup) -> dict[str, bytes]:
    return {k: v for k, v in backup.objects.items() if k.endswith(".json")}


# === Freeze and anchor ===


async def test_finalize_anchors_both_hashes_and_archives_both_files(
    session, finalized, chain, backup
):
    snapshot = await _snapshot(session, finalized)
    expected = canonical_json(metadata_record(finalized))
    expected_hash = hashlib.sha256(expected).hexdigest()

    assert chain.records[str(finalized.id)].data_hash == snapshot.file_sha256
    assert chain.records[f"{finalized.id}:metadata"].data_hash == expected_hash
    assert snapshot.metadata_sha256 == expected_hash
    assert backup.objects[f"{finalized.id}/metadata-{expected_hash}.json"] == expected
    assert f"{finalized.id}/{snapshot.file_sha256}.pdf" in backup.objects


def test_the_same_row_always_gives_the_same_bytes():
    record = {"b": 1, "a": "ñ", "version": 1}

    assert canonical_json(record) == '{"a":"ñ","b":1,"version":1}'.encode()


async def test_the_record_holds_the_fields_fixed_after_finalize(finalized, lawyer, open_book):
    record = metadata_record(finalized)

    assert record == {
        "book_id": str(open_book.id),
        "doc_no": finalized.doc_no,
        "file_name": "Deed",
        "finalized_at": finalized.finalized_at.isoformat(timespec="microseconds"),
        "finalized_by": str(lawyer.id),
        "page_no": finalized.page_no,
        "user_id": str(lawyer.id),
        "version": 1,
    }


async def _finalize_with_failed_metadata_anchor(
    session, intake, chain, backup, lawyer, book, monkeypatch
) -> Document:
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", book.id)
    await mark_processed(session, doc.document_id)
    real_store = chain.store_document_hash

    async def no_metadata(document_id, data_hash):
        if document_id.endswith(":metadata"):
            raise ConnectionError("rpc unreachable")
        return await real_store(document_id, data_hash)

    monkeypatch.setattr(chain, "store_document_hash", no_metadata)
    await DocumentFinalization(session, chain, backup).finalize(me, doc.document_id)
    monkeypatch.setattr(chain, "store_document_hash", real_store)
    return await session.get(Document, doc.document_id)


async def test_failed_metadata_anchor_keeps_finalize_and_retry_uses_the_archive(
    session, intake, storage, chain, backup, archive, lawyer, open_book, monkeypatch
):
    document = await _finalize_with_failed_metadata_anchor(
        session, intake, chain, backup, lawyer, open_book, monkeypatch
    )
    assert document.lifecycle == "FINALIZED"
    assert f"{document.id}:metadata" not in chain.records
    [archived] = _metadata_objects(backup).values()
    document.file_name = "Edited in between"
    await session.flush()

    assert await archive.anchor_pending_metadata() == 1

    assert (
        chain.records[f"{document.id}:metadata"].data_hash
        == hashlib.sha256(archived).hexdigest()
    )
    assert await archive.anchor_pending_metadata() == 0


async def test_retry_counts_a_lost_answer_as_done_if_the_hash_matches(
    session, intake, storage, chain, backup, archive, lawyer, open_book, monkeypatch
):
    document = await _finalize_with_failed_metadata_anchor(
        session, intake, chain, backup, lawyer, open_book, monkeypatch
    )
    real_store = chain.store_document_hash

    async def landed_but_raised(document_id, data_hash):
        await real_store(document_id, data_hash)
        raise RuntimeError("DocumentAlreadyRecorded")

    monkeypatch.setattr(chain, "store_document_hash", landed_but_raised)

    assert await archive.anchor_pending_metadata() == 0
    assert f"{document.id}:metadata" in chain.records


async def test_reconcile_does_not_report_metadata_anchors(
    session, finalized, chain, monkeypatch
):
    monkeypatch.setattr(settings, "CHAIN_SCAN_FROM_BLOCK", 100)

    report = await ChainReconciliation(session, chain).run()

    assert (report.checked, report.ok, report.problems) == (1, 1, [])


# === Detect ===

EDITS = {
    "file_name": lambda d, _: setattr(d, "file_name", "Forged"),
    "doc_no": lambda d, _: setattr(d, "doc_no", 999),
    "page_no": lambda d, _: setattr(d, "page_no", 999),
    "book_id": lambda d, book: setattr(d, "book_id", book.id),
    "finalized_at": lambda d, _: setattr(d, "finalized_at", d.finalized_at.replace(year=2001)),
    "finalized_by": lambda d, _: setattr(d, "finalized_by", None),
}


@pytest.mark.parametrize("field", EDITS)
async def test_each_edited_field_is_named_on_verify_and_in_the_scheduled_job(
    field, session, finalized, verifier, archive, lawyer, closed_book, alert_emails
):
    EDITS[field](finalized, closed_book)
    await session.flush()

    result = await verifier.verify_document(as_auth(lawyer), finalized.id)
    report = await archive.verify()

    assert result.status == "AUTHENTIC"
    assert result.metadata_status == "ALTERED"
    assert [c.field for c in result.metadata_changes] == [field]
    assert result.needs_restore is True
    assert [(p.kind, p.field) for p in report.records.problems] == [("metadata", field)]
    assert report.records.altered == 1


async def test_edited_owner_is_named(session, finalized, verifier, archive, other_lawyer, lawyer):
    finalized.user_id = other_lawyer.id
    await session.flush()

    result = await verifier.verify_document(as_auth(other_lawyer), finalized.id)
    report = await archive.verify()

    assert [c.field for c in result.metadata_changes] == ["user_id"]
    # The alert follows the anchored owner, not the one written over it.
    [alert] = await _notifications(session, NotificationType.RECORD_ALTERED)
    assert alert.user_id == lawyer.id
    assert report.records.altered == 1


async def test_a_change_names_the_anchored_and_current_values(
    session, finalized, verifier, lawyer
):
    finalized.file_name = "Forged"
    await session.flush()

    result = await verifier.verify_document(as_auth(lawyer), finalized.id, tamper_report=False)

    [change] = result.metadata_changes
    assert (change.anchored, change.current) == ("Deed", "Forged")


async def test_untouched_record_matches(finalized, verifier, lawyer):
    result = await verifier.verify_document(as_auth(lawyer), finalized.id)

    assert result.metadata_status == "MATCH"
    assert result.metadata_changes == []
    assert result.needs_restore is False


async def test_parties_changing_raises_nothing(
    session, finalized, verifier, archive, client, lawyer
):
    await add_party(session, finalized.id, client)
    await archive.verify()
    await session.execute(delete(DocumentParty).where(DocumentParty.document_id == finalized.id))

    result = await verifier.verify_document(as_auth(lawyer), finalized.id)
    report = await archive.verify()

    assert result.metadata_status == "MATCH"
    assert report.records.problems == []
    assert await _notifications(session, NotificationType.RECORD_ALTERED) == []


async def _anchor_record_finalized_by(session, document, chain, backup, user) -> None:
    """Only the owner can finalize, and deleting the owner deletes the document,
    so a document finalized by someone else is set up by hand."""
    record = metadata_record(document) | {"finalized_by": str(user.id)}
    data = canonical_json(record)
    data_hash = hashlib.sha256(data).hexdigest()
    backup.objects[f"{document.id}/metadata-{data_hash}.json"] = data
    await chain.store_document_hash(f"{document.id}:metadata", data_hash)
    document.finalized_by = user.id
    await session.flush()


async def test_deleting_the_user_who_finalized_raises_nothing(
    session, finalized, chain, backup, verifier, archive, other_lawyer, lawyer
):
    await _anchor_record_finalized_by(session, finalized, chain, backup, other_lawyer)
    await session.execute(delete(User).where(User.id == other_lawyer.id))
    await session.refresh(finalized)
    assert finalized.finalized_by is None

    result = await verifier.verify_document(as_auth(lawyer), finalized.id)
    report = await archive.verify()

    assert result.metadata_status == "MATCH"
    assert report.records.problems == []


async def test_clearing_finalized_by_while_the_user_exists_is_altered(
    session, finalized, chain, backup, verifier, other_lawyer, lawyer
):
    await _anchor_record_finalized_by(session, finalized, chain, backup, other_lawyer)
    finalized.finalized_by = None
    await session.flush()

    result = await verifier.verify_document(as_auth(lawyer), finalized.id)

    assert result.metadata_status == "ALTERED"
    assert [c.field for c in result.metadata_changes] == ["finalized_by"]


async def test_no_metadata_anchor_is_not_anchored_not_altered(
    session, finalized, chain, verifier, archive, lawyer
):
    del chain.records[f"{finalized.id}:metadata"]
    finalized.file_name = "Renamed before the anchor existed"
    await session.flush()

    result = await verifier.verify_document(as_auth(lawyer), finalized.id)
    report = await archive.verify()

    assert result.metadata_status == "NOT_ANCHORED"
    assert report.records.not_anchored == 1
    assert report.records.problems == []


async def test_an_unreadable_archive_on_a_mismatch_is_unknown(
    session, finalized, backup, verifier, archive, lawyer, monkeypatch
):
    finalized.file_name = "Forged"
    await session.flush()

    async def down(_key):
        raise ConnectionError("bucket unreachable")

    monkeypatch.setattr(backup, "exists", down)

    result = await verifier.verify_document(as_auth(lawyer), finalized.id)
    await archive.verify()

    assert result.metadata_status == "UNAVAILABLE"
    assert await _notifications(session, NotificationType.RECORD_ALTERED) == []


async def test_lifecycle_edit_is_a_record_finding(session, finalized, verifier, archive, lawyer):
    finalized.lifecycle = "SIGNED"
    await session.flush()

    result = await verifier.verify_document(as_auth(lawyer), finalized.id)
    report = await archive.verify()

    assert result.status == "AUTHENTIC"
    assert result.needs_restore is True
    assert [(p.kind, p.field) for p in report.records.problems] == [("lifecycle", "lifecycle")]


async def test_snapshot_only_edit_notifies_once_and_the_pdf_stays_authentic(
    session, finalized, verifier, archive, lawyer, alert_emails
):
    snapshot = await _snapshot(session, finalized)
    snapshot.file_sha256 = "0" * 64
    await session.flush()

    first = await archive.verify()
    await archive.verify()

    result = await verifier.verify_document(as_auth(lawyer), finalized.id)
    assert result.status == "AUTHENTIC"
    assert first.live.ok == 1
    assert [p.field for p in first.records.problems] == ["file_sha256"]
    alerts = await _notifications(session, NotificationType.RECORD_ALTERED)
    assert len(alerts) == 1
    assert alerts[0].user_id == lawyer.id


async def test_snapshot_only_edit_raises_no_archive_alert(
    session, finalized, archive, alert_emails
):
    snapshot = await _snapshot(session, finalized)
    snapshot.file_sha256 = "0" * 64
    await session.flush()

    report = await archive.verify()

    assert (report.ok, report.mismatch) == (1, 0)
    assert await _notifications(session, NotificationType.ARCHIVE_PROBLEM) == []
    assert alert_emails == []


async def test_edited_size_and_key_are_found_against_the_archive(session, finalized, archive):
    snapshot = await _snapshot(session, finalized)
    snapshot.size_bytes += 1
    snapshot.backup_key = "elsewhere.pdf"
    await session.flush()

    report = await archive.verify()

    assert report.ok == 1
    assert {p.field for p in report.records.problems} == {"size_bytes", "backup_key"}


async def test_chain_outage_produces_no_alert_and_no_finding(
    session, finalized, chain, archive, monkeypatch, alert_emails
):
    finalized.file_name = "Forged"
    await session.flush()
    _break_chain(monkeypatch, chain)

    report = await archive.verify()

    assert report.records.unavailable == 1
    assert report.records.problems == []
    assert await _notifications(session, NotificationType.RECORD_ALTERED) == []
    assert await _notifications(session, NotificationType.ARCHIVE_PROBLEM) == []
    assert alert_emails == []
    rows = await session.execute(
        select(VerificationResult).where(VerificationResult.status == "RECORD_ALTERED")
    )
    assert rows.scalars().all() == []


# === Alert and report ===


async def test_altered_value_alerts_once_and_a_new_value_alerts_again(
    session, finalized, archive, lawyer
):
    finalized.file_name = "Forged"
    await session.flush()
    await archive.verify()
    await archive.verify()

    [alert] = await _notifications(session, NotificationType.RECORD_ALTERED)
    assert alert.user_id == lawyer.id
    assert alert.body == "`file_name` of Forged was changed from Deed to Forged."

    finalized.file_name = "Forged again"
    await session.flush()
    await archive.verify()

    assert len(await _notifications(session, NotificationType.RECORD_ALTERED)) == 2


async def test_findings_reach_the_integrity_report(session, finalized, archive, lawyer):
    finalized.file_name = "Forged"
    await session.flush()
    await archive.verify()
    await archive.verify()

    report = await IntegrityReport(session).for_owner(lawyer.id)

    assert report.summary.record_altered == 1
    [row] = report.results
    assert (row.status, row.is_authentic) == ("RECORD_ALTERED", True)


# === Archive alerts ===


async def test_deleted_archive_notifies_once_and_emails_the_backup_owner(
    session, finalized, backup, archive, lawyer, alert_emails
):
    snapshot = await _snapshot(session, finalized)
    del backup.objects[snapshot.backup_key]

    await archive.verify()
    report = await archive.verify()

    assert report.missing == 1
    [alert] = await _notifications(session, NotificationType.ARCHIVE_PROBLEM)
    assert alert.user_id == lawyer.id
    assert "Restore won't work" in alert.body
    assert [subject for subject, _ in alert_emails] == ["Recovery copy missing"]


async def test_a_bad_archive_alerts_as_a_mismatch(session, finalized, backup, archive, alert_emails):
    snapshot = await _snapshot(session, finalized)
    backup.objects[snapshot.backup_key] = b"damaged"

    report = await archive.verify()

    assert report.mismatch == 1
    [alert] = await _notifications(session, NotificationType.ARCHIVE_PROBLEM)
    assert alert.event_metadata["finding"] == "mismatch"


async def test_storage_outage_is_unavailable_with_no_alert(
    session, finalized, backup, archive, monkeypatch, alert_emails
):
    async def down(_key):
        raise ConnectionError("bucket unreachable")

    monkeypatch.setattr(backup, "exists", down)

    report = await archive.verify()

    assert (report.unavailable, report.missing) == (1, 0)
    assert await _notifications(session, NotificationType.ARCHIVE_PROBLEM) == []
    assert alert_emails == []


@pytest.mark.parametrize("damage", ["deleted", "corrupted"])
async def test_a_lost_metadata_archive_alerts_once_on_an_untouched_row(
    damage, session, finalized, backup, archive, lawyer, alert_emails
):
    [key] = _metadata_objects(backup)
    if damage == "deleted":
        del backup.objects[key]
    else:
        backup.objects[key] = b"damaged"

    await archive.verify()
    await archive.verify()

    [alert] = await _notifications(session, NotificationType.ARCHIVE_PROBLEM)
    assert alert.user_id == lawyer.id
    assert "Restore won't work" in alert.body
    assert len(alert_emails) == 1


async def test_a_failed_email_does_not_fail_the_check(
    session, finalized, backup, archive, monkeypatch
):
    snapshot = await _snapshot(session, finalized)
    del backup.objects[snapshot.backup_key]

    async def bounced(subject, body):
        raise ConnectionError("mail down")

    monkeypatch.setattr("app.features.snapshot.backups.send_backup_alert", bounced)

    report = await archive.verify()

    assert report.missing == 1


# === Restore ===


async def test_one_restore_fixes_lifecycle_snapshot_pdf_and_metadata_together(
    session, finalized, storage, verifier, restoration, chain, lawyer
):
    anchored = chain.records[str(finalized.id)].data_hash
    snapshot = await _snapshot(session, finalized)
    original_copy = snapshot.signed_copy_id
    [url] = storage.objects
    storage.objects[url] += b" tampered"
    snapshot.file_sha256 = "0" * 64
    snapshot.size_bytes = 1
    snapshot.backup_key = "elsewhere.pdf"
    snapshot.signed_copy_id = original_copy
    finalized.lifecycle = "SIGNED"
    finalized.file_name = "Forged"
    finalized.doc_no = 999
    await session.flush()

    result = await restoration.restore(as_auth(lawyer), finalized.id)

    assert result.restored is True
    assert [(r.record, r.field) for r in result.repaired] == [
        ("lifecycle", "lifecycle"),
        ("snapshot", "file_sha256"),
        ("snapshot", "backup_key"),
        ("snapshot", "size_bytes"),
        ("metadata", "doc_no"),
        ("metadata", "file_name"),
    ]
    assert result.unrepaired == []
    after = await verifier.verify_document(as_auth(lawyer), finalized.id)
    assert after.status == "AUTHENTIC"
    assert after.baseline_trusted is True
    assert after.metadata_status == "MATCH"
    assert after.needs_restore is False
    assert finalized.lifecycle == "FINALIZED"
    assert finalized.file_name == "Deed"
    assert snapshot.file_sha256 == anchored


@pytest.mark.parametrize(
    "edit",
    [
        lambda d, s: setattr(d, "lifecycle", "SIGNED"),
        lambda d, s: setattr(s, "file_sha256", "0" * 64),
        lambda d, s: setattr(s, "size_bytes", 1),
        lambda d, s: setattr(d, "file_name", "Forged"),
    ],
    ids=["lifecycle", "snapshot", "size", "metadata"],
)
async def test_restore_runs_for_a_record_finding_on_a_good_pdf(
    edit, session, finalized, storage, restoration, lawyer, queries
):
    snapshot = await _snapshot(session, finalized)
    edit(finalized, snapshot)
    await session.flush()
    objects = dict(storage.objects)

    result = await restoration.restore(as_auth(lawyer), finalized.id)

    assert result.restored is True
    assert result.previous_hash is None
    assert result.verification.needs_restore is False
    assert storage.objects == objects
    copies = (await queries.get_signed_copies(as_auth(lawyer), finalized.id)).copies
    assert len(copies) == 1


async def test_every_change_is_in_the_history(session, finalized, restoration, queries, lawyer):
    finalized.lifecycle = "SIGNED"
    finalized.file_name = "Forged"
    await session.flush()

    await restoration.restore(as_auth(lawyer), finalized.id)

    last = (await queries.get_history(as_auth(lawyer), finalized.id)).changes[-1]
    assert (last.action, last.from_stage, last.to_stage) == ("restore", "SIGNED", "FINALIZED")
    assert [c.model_dump() for c in last.changes] == [
        {"record": "lifecycle", "field": "lifecycle", "old": "SIGNED", "new": "FINALIZED"},
        {"record": "metadata", "field": "file_name", "old": "Forged", "new": "Deed"},
    ]


async def test_snapshot_copy_comes_from_the_row_matching_the_chain(
    session, finalized, intake, restoration, lawyer, open_book
):
    from app.features.documents.models import SignedCopy

    snapshot = await _snapshot(session, finalized)
    genuine = snapshot.signed_copy_id
    other = await intake.upload_signed(as_auth(lawyer), pdf("other"), "Other", open_book.id)
    other_doc = await session.get(Document, other.document_id)
    snapshot.signed_copy_id = other_doc.signed_copy_id
    await session.flush()

    result = await restoration.restore(as_auth(lawyer), finalized.id)

    assert snapshot.signed_copy_id == genuine
    assert ("snapshot", "signed_copy_id") in [(r.record, r.field) for r in result.repaired]
    assert isinstance(await session.get(SignedCopy, genuine), SignedCopy)


async def test_no_copy_matching_the_chain_leaves_the_column_and_says_so(
    session, finalized, restoration, lawyer
):
    from app.features.documents.models import SignedCopy

    snapshot = await _snapshot(session, finalized)
    copy = await session.get(SignedCopy, snapshot.signed_copy_id)
    copy.sha256 = "0" * 64
    await session.flush()
    before = snapshot.signed_copy_id

    result = await restoration.restore(as_auth(lawyer), finalized.id)

    assert snapshot.signed_copy_id == before
    assert result.unrepaired == ["snapshot.signed_copy_id"]


async def test_restore_refuses_an_archived_json_that_does_not_match_the_anchor(
    session, finalized, backup, restoration, lawyer
):
    [key] = _metadata_objects(backup)
    backup.objects[key] = canonical_json({"file_name": "Forged", "version": 1})
    finalized.file_name = "Forged"
    await session.flush()

    with pytest.raises(ConflictError) as refused:
        await restoration.restore(as_auth(lawyer), finalized.id)

    assert refused.value.details["reason"] == "BACKUP_MISMATCH"
    assert finalized.file_name == "Forged"


async def test_a_forged_restore_writes_nothing_the_chain_does_not_support(
    session, finalized, backup, chain, restoration, lawyer
):
    """An archive planted under the attacker's own hash is never looked at, and
    neither is the row's `metadata_sha256`."""
    forged = json.loads(next(iter(_metadata_objects(backup).values())))
    forged["file_name"] = "Forged"
    data = canonical_json(forged)
    forged_hash = hashlib.sha256(data).hexdigest()
    backup.objects[f"{finalized.id}/metadata-{forged_hash}.json"] = data
    snapshot = await _snapshot(session, finalized)
    snapshot.metadata_sha256 = forged_hash
    finalized.file_name = "Forged"
    await session.flush()

    result = await restoration.restore(as_auth(lawyer), finalized.id)

    assert finalized.file_name == "Deed"
    assert result.verification.metadata_status == "MATCH"
    assert forged_hash != chain.records[f"{finalized.id}:metadata"].data_hash


async def test_restore_with_a_chain_outage_writes_nothing(
    session, finalized, chain, restoration, lawyer, monkeypatch
):
    finalized.file_name = "Forged"
    await session.flush()
    _break_chain(monkeypatch, chain)

    with pytest.raises(ServiceUnavailableError):
        await restoration.restore(as_auth(lawyer), finalized.id)

    assert finalized.file_name == "Forged"


async def test_a_lifecycle_outside_the_known_stages_does_not_block_restore(
    session, finalized, restoration, lawyer
):
    finalized.lifecycle = "X"
    await session.flush()

    result = await restoration.restore(as_auth(lawyer), finalized.id)

    assert result.restored is True
    assert finalized.lifecycle == "FINALIZED"


async def test_a_row_claiming_the_chain_hash_is_not_trusted_as_the_copy(
    session, finalized, storage, intake, restoration, lawyer, open_book
):
    from app.features.documents.models import SignedCopy

    snapshot = await _snapshot(session, finalized)
    genuine = await session.get(SignedCopy, snapshot.signed_copy_id)
    anchored = genuine.sha256
    forged_url = storage.locator("forged.pdf")
    storage.objects[forged_url] = b"forged"
    forged = SignedCopy(
        document_id=finalized.id,
        storage_url=forged_url,
        sha256=anchored,
        content_type="application/pdf",
        size_bytes=6,
        original_filename="forged.pdf",
    )
    session.add(forged)
    genuine.sha256 = "0" * 64
    await session.flush()

    result = await restoration.restore(as_auth(lawyer), finalized.id)

    assert snapshot.signed_copy_id == genuine.id
    assert result.unrepaired == ["snapshot.signed_copy_id"]


async def test_restore_clears_finalized_by_when_the_anchored_user_is_gone(
    session, finalized, chain, backup, restoration, other_lawyer, client, lawyer
):
    await _anchor_record_finalized_by(session, finalized, chain, backup, other_lawyer)
    finalized.finalized_by = client.id
    await session.flush()
    await session.execute(delete(User).where(User.id == other_lawyer.id))

    result = await restoration.restore(as_auth(lawyer), finalized.id)

    assert result.restored is True
    assert finalized.finalized_by is None
    assert result.verification.metadata_status == "MATCH"


async def test_the_full_verify_offers_restore_for_a_size_edit(
    session, finalized, verifier, lawyer
):
    snapshot = await _snapshot(session, finalized)
    snapshot.size_bytes += 1
    await session.flush()

    result = await verifier.verify_document(as_auth(lawyer), finalized.id)

    assert result.needs_restore is True


async def test_can_restore_is_offered_whatever_the_row_says(
    session, finalized, lawyer, client
):
    from app.features.documents.access import DocumentAccess

    finalized.lifecycle = "SIGNED"
    finalized.on_chain = False
    access = DocumentAccess(session)

    assert access.permissions(as_auth(lawyer), finalized).can_restore is True
    assert access.permissions(as_auth(client), finalized).can_restore is False


