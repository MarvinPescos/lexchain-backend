"""Deleted documents: the trigger, the two layers behind it, and recovery."""

import importlib.util
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.core import settings
from app.features.books.models import Book
from app.features.chunks.models import DocumentChunk
from app.features.documents.extraction.models import DocumentExtraction
from app.features.documents.models import (
    DeletedDocument,
    Document,
    DocumentStageChange,
)
from app.features.documents.workflow.deletion_watch import DeletionWatch, ledger_key
from app.features.documents.workflow.recovery import RECOVERED_TABLES, DocumentRecovery
from app.features.documents.workflow.restoration import DocumentRestoration
from app.features.documents.workflow.tamper_reports import TamperReports
from app.features.notifications.models import Notification, NotificationType
from app.features.users.models import User
from app.infrastructure.database import Base
from app.infrastructure.nlp.embeddings.base import EMBEDDING_DIM
from app.shared.errors.exceptions import ConflictError, NotFoundError
from tests.conftest import as_auth, pdf
from tests.features.blockchain.test_tamper_report import FakeBackup

MIGRATION = (
    Path(__file__).parents[3] / "migrations/versions/a4e1d7c2b906_feat_deletion_watch.py"
)


def migration():
    spec = importlib.util.spec_from_file_location("deletion_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def trigger(session):
    """The real trigger, from the migration. Tests build the schema from the
    models, so it isn't there otherwise; leaving this out is the trigger being
    turned off."""
    await session.execute(text(migration().CREATE_FUNCTION))
    await session.execute(text(migration().CREATE_TRIGGER))


@pytest.fixture
def backup() -> FakeBackup:
    return FakeBackup()


@pytest.fixture
def watch(session, storage, backup) -> DeletionWatch:
    return DeletionWatch(session, storage, backup)


@pytest.fixture
def reports(session, chain, backup) -> TamperReports:
    return TamperReports(DocumentRestoration(session, chain, backup))


@pytest.fixture
async def document_id(session, intake, queue, storage, lawyer, open_book):
    """An uploaded document, not finalized."""
    uploaded = await intake.upload_signed(
        as_auth(lawyer), pdf("Draft"), "Draft", open_book.id
    )
    await session.commit()
    return uploaded.document_id


def forget_documents(session):
    """Raw SQL went around the session, so drop what it holds of the document."""
    for loaded in list(session.identity_map.values()):
        if not isinstance(loaded, (User, Book)):
            session.expunge(loaded)


async def delete(session, document_id):
    await session.execute(
        text("DELETE FROM documents WHERE id = :id"), {"id": document_id}
    )
    forget_documents(session)


async def alerts(session):
    result = await session.execute(
        select(Notification).where(Notification.type == NotificationType.DOCUMENT_DELETED)
    )
    return list(result.scalars().all())


async def records(session):
    result = await session.execute(select(DeletedDocument))
    return list(result.scalars().all())


def test_every_child_table_is_saved_and_recovered():
    """A new table pointing at documents must be added to both lists."""
    children = {
        t.name
        for t in Base.metadata.sorted_tables
        for fk in t.foreign_keys
        if fk.column.table.name in ("documents", "draft_comments", "ecopy_requests")
    }
    # RESTRICT: a document with a snapshot can't be deleted until it's removed.
    assert children - {"document_snapshots", "documents"} == set(RECOVERED_TABLES)
    for table in RECOVERED_TABLES:
        assert f"'{table}'" in migration().CREATE_FUNCTION


# === Layer 1: the trigger ===


async def test_delete_saves_the_row_and_its_children(
    session, trigger, document_id, lawyer
):
    await delete(session, document_id)

    [saved] = await records(session)
    assert saved.document_id == document_id
    assert saved.user_id == lawyer.id
    assert saved.source == "trigger"
    assert saved.document["file_name"] == "Draft"
    assert len(saved.related["signed_copies"]) == 1
    assert saved.cascaded is False


async def test_watch_alerts_the_owner_once(session, trigger, watch, document_id, lawyer):
    await delete(session, document_id)

    await watch.run()
    await watch.run()

    [alert] = await alerts(session)
    assert alert.user_id == lawyer.id
    assert alert.event_metadata["document_id"] == str(document_id)
    assert alert.event_metadata["source"] == "trigger"


async def test_a_trigger_finding_is_not_reported_again_by_the_other_layers(
    session, trigger, watch, document_id
):
    await watch.run()  # writes the ledger
    await delete(session, document_id)

    await watch.run()

    assert len(await records(session)) == 1
    assert len(await alerts(session)) == 1


async def test_owner_gone_alerts_every_lawyer(
    session, trigger, watch, document_id, lawyer
):
    await delete(session, document_id)
    [saved] = await records(session)
    saved.user_id = uuid.uuid4()
    await session.commit()

    await watch.run()

    assert [a.user_id for a in await alerts(session)] == [lawyer.id]


async def test_deleted_with_its_book_is_marked_cascaded(
    session, trigger, document_id, open_book
):
    book_id = open_book.id
    await session.execute(text("DELETE FROM books WHERE id = :id"), {"id": book_id})
    session.expunge(open_book)
    forget_documents(session)

    [saved] = await records(session)
    assert saved.cascaded is True


# === Recovery ===


async def test_recover_puts_everything_back(
    session, trigger, document_id, storage, lawyer
):
    copy_id = await session.scalar(
        select(Document.signed_copy_id).where(Document.id == document_id)
    )
    extraction = DocumentExtraction(
        document_id=document_id, engine="test", text="body", page_count=1, meta={}
    )
    session.add(extraction)
    await session.flush()
    session.add(
        DocumentChunk(
            document_id=document_id,
            extraction_id=extraction.id,
            chunk_index=0,
            text="body",
            char_count=4,
            embedding_vec=[0.5] * EMBEDDING_DIM,
            strategy="test",
            engine="test",
        )
    )
    await session.commit()
    await delete(session, document_id)

    result = await DocumentRecovery(session).recover(as_auth(lawyer), document_id)

    assert result.file_present is True
    document = await session.get(Document, document_id)
    assert document is not None
    assert document.file_name == "Draft"
    assert document.signed_copy_id == copy_id
    chunk = await session.scalar(
        select(DocumentChunk).where(DocumentChunk.document_id == document_id)
    )
    assert chunk is not None and chunk.embedding_vec[0] == 0.5
    logged = await session.scalars(
        select(DocumentStageChange.action).where(
            DocumentStageChange.document_id == document_id
        )
    )
    assert "recover" in list(logged)


async def test_recover_says_when_the_pdf_is_gone_too(
    session, trigger, document_id, storage, lawyer
):
    await delete(session, document_id)
    storage.objects.clear()

    result = await DocumentRecovery(session).recover(as_auth(lawyer), document_id)

    assert result.file_present is False


async def test_recovered_document_can_be_recovered_only_once(
    session, trigger, document_id, lawyer
):
    await delete(session, document_id)
    recovery = DocumentRecovery(session)
    await recovery.recover(as_auth(lawyer), document_id)

    with pytest.raises(NotFoundError):
        await recovery.recover(as_auth(lawyer), document_id)


async def test_only_the_owner_can_recover(session, trigger, document_id, other_lawyer):
    await delete(session, document_id)

    with pytest.raises(NotFoundError):
        await DocumentRecovery(session).recover(as_auth(other_lawyer), document_id)


async def test_recover_refuses_when_its_book_is_gone(
    session, trigger, document_id, open_book, lawyer
):
    book_id = open_book.id
    await session.execute(text("DELETE FROM books WHERE id = :id"), {"id": book_id})
    session.expunge(open_book)
    forget_documents(session)

    with pytest.raises(ConflictError) as refused:
        await DocumentRecovery(session).recover(as_auth(lawyer), document_id)

    assert refused.value.details["reason"] == "RECOVERY_CONFLICT"
    assert await session.get(Document, document_id) is None


async def test_a_database_restore_closes_the_record(
    session, trigger, watch, document_id, lawyer
):
    await delete(session, document_id)
    await DocumentRecovery(session).recover(as_auth(lawyer), document_id)
    await delete(session, document_id)
    await DocumentRecovery(session).recover(as_auth(lawyer), document_id)
    second = (await records(session))[1]
    second.recovered_at = None
    await session.commit()

    await watch.run()
    forget_documents(session)

    assert all(r.recovered_at is not None for r in await records(session))


# === Tamper Reports ===


async def test_deleted_document_is_listed_and_restorable(
    session, trigger, reports, document_id, lawyer
):
    await delete(session, document_id)

    listed = await reports.list_open(as_auth(lawyer))

    [report] = listed.reports
    assert report.document_id == document_id
    assert report.status == "DELETED"
    assert report.file_name == "Draft"
    assert report.restorable is True
    assert [c.kind for c in report.changes] == ["deleted"]


async def test_restore_all_recovers_deleted_documents(
    session, trigger, reports, document_id, lawyer
):
    await delete(session, document_id)

    result = await reports.restore_all(as_auth(lawyer))

    assert [r.document_id for r in result.recovered] == [document_id]
    assert result.failed == []
    assert (await reports.list_open(as_auth(lawyer))).total == 0


async def test_restore_all_reports_a_deletion_that_needs_a_backup(
    session, watch, reports, document_id, lawyer
):
    await watch.run()
    await delete(session, document_id)
    await watch.run()

    result = await reports.restore_all(as_auth(lawyer))

    assert [f.document_id for f in result.failed] == [document_id]


# === Layer 2: the ledger, with the trigger off ===


async def test_ledger_catches_a_delete_the_trigger_missed(
    session, watch, document_id, lawyer, reports
):
    await watch.run()
    await delete(session, document_id)

    await watch.run()
    await watch.run()

    [found] = await records(session)
    assert found.source == "ledger"
    assert found.file_name == "Draft"
    assert found.document is None
    [alert] = await alerts(session)
    assert alert.user_id == lawyer.id
    [report] = (await reports.list_open(as_auth(lawyer))).reports
    assert report.restorable is False
    assert report.blocked_reason == "backup_needed"


async def test_recover_refuses_with_no_saved_copy(session, watch, document_id, lawyer):
    await watch.run()
    await delete(session, document_id)
    await watch.run()

    with pytest.raises(ConflictError) as refused:
        await DocumentRecovery(session).recover(as_auth(lawyer), document_id)

    assert refused.value.details["reason"] == "BACKUP_NEEDED"


async def test_first_run_only_writes_the_ledger(session, watch, backup, document_id):
    await watch.run()

    [saved] = backup.objects.values()
    assert [d["id"] for d in json.loads(saved)["documents"]] == [str(document_id)]
    assert await alerts(session) == []


async def test_a_second_run_in_the_hour_keeps_the_first_ledger(
    session, watch, backup, document_id
):
    await watch.run()
    first = dict(backup.objects)
    await delete(session, document_id)

    await watch.run()

    assert backup.objects == first


async def test_ledger_from_an_earlier_hour_is_found(
    session, watch, backup, document_id, lawyer
):
    earlier = datetime.now(timezone.utc) - timedelta(hours=5)
    entry = {"id": str(uuid.uuid4()), "user_id": str(lawyer.id), "file_name": "Old"}
    backup.objects[ledger_key(earlier)] = json.dumps({"documents": [entry]}).encode()

    await watch.run()

    [found] = await records(session)
    assert found.file_name == "Old"


async def test_unreadable_ledger_alerts_nothing_and_storage_still_runs(
    session, watch, backup, document_id, storage, monkeypatch
):
    await watch.run()
    await delete(session, document_id)

    async def outage(key):
        raise OSError("down")

    monkeypatch.setattr(backup, "exists", outage)

    await watch.run()

    [found] = await records(session)
    assert found.source == "storage"


# === Layer 3: stored files, with the trigger off and no ledger ===


async def test_stored_file_with_no_row_alerts(session, storage, document_id, lawyer):
    await delete(session, document_id)

    await DeletionWatch(session, storage, None).run()

    [found] = await records(session)
    assert found.source == "storage"
    assert found.user_id == lawyer.id
    assert len(await alerts(session)) == 1


async def test_a_fresh_upload_is_left_alone(session, storage, document_id):
    await delete(session, document_id)
    storage.uploaded_at = datetime.now(timezone.utc)

    await DeletionWatch(session, storage, None).run()

    assert await records(session) == []


async def test_files_from_before_the_watch_are_left_alone(
    session, storage, document_id, monkeypatch
):
    await delete(session, document_id)
    monkeypatch.setattr(
        settings, "DELETION_ORPHANS_SINCE", datetime.now(timezone.utc)
    )

    await DeletionWatch(session, storage, None).run()

    assert await records(session) == []


async def test_a_live_document_is_not_an_orphan(session, storage, document_id):
    await DeletionWatch(session, storage, None).run()

    assert await records(session) == []


async def test_storage_outage_alerts_nothing(
    session, storage, document_id, monkeypatch
):
    await delete(session, document_id)

    async def outage():
        raise OSError("down")

    monkeypatch.setattr(storage, "list_document_folders", outage)

    await DeletionWatch(session, storage, None).run()

    assert await records(session) == []
