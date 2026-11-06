"""Restore: a TAMPERED finalized document gets its anchored bytes back from the archive."""

import hashlib

import httpx
import pytest

from app.features.blockchain.service import BlockchainService
from app.features.documents.models import Document
from app.features.documents.workflow.finalization import DocumentFinalization
from app.features.documents.workflow.restoration import DocumentRestoration
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.shared.errors.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
)
from tests.conftest import as_auth, pdf, mark_processed
from tests.features.blockchain.test_tamper_report import FakeBackup
from tests.features.documents.test_drafting import add_party


@pytest.fixture
def backup() -> FakeBackup:
    return FakeBackup()


@pytest.fixture
def restoration(session, storage, chain, backup) -> DocumentRestoration:
    return DocumentRestoration(session, chain, backup)


@pytest.fixture
def verifier(session, storage, chain) -> BlockchainService:
    return BlockchainService(session, chain)


@pytest.fixture
async def finalized(session, intake, queue, chain, backup, storage, lawyer, open_book):
    """A finalized, archived deed; returns (document_id, stored url, anchored hash)."""
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    record = await DocumentFinalization(session, chain, backup).finalize(
        me, doc.document_id
    )
    [url] = storage.objects
    return doc.document_id, url, record.data_hash


@pytest.fixture
async def tampered(finalized, storage):
    document_id, url, anchored = finalized
    storage.objects[url] += b" tampered"
    return document_id, url, anchored


async def test_restore_makes_the_anchored_bytes_current_again(
    restoration, verifier, tampered, storage, lawyer
):
    document_id, url, anchored = tampered
    tampered_hash = hashlib.sha256(storage.objects[url]).hexdigest()

    result = await restoration.restore(as_auth(lawyer), document_id)

    assert result.restored is True
    assert result.previous_hash == tampered_hash
    assert result.restored_hash == anchored
    assert result.restored_by == lawyer.id
    assert result.verification.status == "AUTHENTIC"
    assert result.verification.current_hash == result.verification.onchain_hash
    after = await verifier.verify_document(as_auth(lawyer), document_id)
    assert after.status == "AUTHENTIC"
    assert after.current_hash == anchored


async def test_tampered_file_is_kept_in_signed_copy_history(
    restoration, queries, tampered, storage, lawyer
):
    document_id, url, anchored = tampered
    tampered_bytes = storage.objects[url]

    result = await restoration.restore(as_auth(lawyer), document_id)

    copies = (await queries.get_signed_copies(as_auth(lawyer), document_id)).copies
    restored, preserved, original = copies
    assert restored.id == result.restored_copy_id and restored.is_current
    assert restored.sha256 == anchored
    assert preserved.id == result.preserved_copy_id
    assert preserved.sha256 == hashlib.sha256(tampered_bytes).hexdigest()
    assert not original.is_current
    preserved_url = next(u for u, b in storage.objects.items() if u != url and b == tampered_bytes)
    assert storage.objects[preserved_url] == tampered_bytes


async def test_restore_is_recorded_in_the_history(
    restoration, queries, tampered, storage, lawyer
):
    document_id, url, anchored = tampered
    tampered_hash = hashlib.sha256(storage.objects[url]).hexdigest()

    await restoration.restore(as_auth(lawyer), document_id)

    changes = (await queries.get_history(as_auth(lawyer), document_id)).changes
    last = changes[-1]
    assert last.action == "restore"
    assert (last.from_stage, last.to_stage) == ("FINALIZED", "FINALIZED")
    assert last.actor_id == lawyer.id
    assert (last.from_hash, last.to_hash) == (tampered_hash, anchored)
    assert last.created_at is not None


async def test_restore_keeps_the_anchor_and_lifecycle(
    session, restoration, tampered, chain, lawyer
):
    document_id, _, anchored = tampered
    records_before = dict(chain.records)

    await restoration.restore(as_auth(lawyer), document_id)

    document = await session.get(Document, document_id)
    assert document.lifecycle == "FINALIZED"
    assert document.document_hash == anchored
    assert chain.records == records_before


async def test_retry_after_restore_is_a_no_op(
    restoration, queries, tampered, chain, storage, lawyer
):
    document_id, _, _ = tampered
    first = await restoration.restore(as_auth(lawyer), document_id)
    objects = dict(storage.objects)
    records = dict(chain.records)

    again = await restoration.restore(as_auth(lawyer), document_id)

    assert again.restored is False
    assert again.restored_copy_id == first.restored_copy_id
    assert again.verification.status == "AUTHENTIC"
    assert storage.objects == objects
    assert chain.records == records
    copies = (await queries.get_signed_copies(as_auth(lawyer), document_id)).copies
    assert len(copies) == 3
    history = (await queries.get_history(as_auth(lawyer), document_id)).changes
    assert [c.action for c in history].count("restore") == 1


async def test_authentic_document_is_left_alone(
    restoration, queries, finalized, storage, lawyer
):
    document_id, _, _ = finalized
    objects = dict(storage.objects)

    result = await restoration.restore(as_auth(lawyer), document_id)

    assert result.restored is False
    assert result.verification.status == "AUTHENTIC"
    assert storage.objects == objects


# === Refusals: nothing changes ===


async def _assert_untouched(queries, lawyer, document_id, storage, objects):
    copies = (await queries.get_signed_copies(as_auth(lawyer), document_id)).copies
    assert len(copies) == 1 and copies[0].is_current
    assert storage.objects == objects


async def _alter_snapshot(session, document_id, **fields):
    snapshot = await DocumentSnaphotRepository(session).get_by_document_id(document_id)
    for name, value in fields.items():
        setattr(snapshot, name, value)
    await session.flush()


async def test_altered_snapshot_does_not_block_restore(
    session, restoration, tampered, storage, lawyer
):
    document_id, url, anchored = tampered
    await _alter_snapshot(session, document_id, file_sha256="0" * 64)

    result = await restoration.restore(as_auth(lawyer), document_id)

    assert result.restored is True
    assert result.restored_hash == anchored
    assert result.verification.status == "AUTHENTIC"
    assert result.verification.baseline_trusted is True
    assert anchored in {hashlib.sha256(b).hexdigest() for b in storage.objects.values()}


async def test_altered_snapshot_and_deleted_file_are_restored(
    session, restoration, finalized, storage, lawyer
):
    document_id, url, anchored = finalized
    await _alter_snapshot(session, document_id, file_sha256="0" * 64)
    del storage.objects[url]

    result = await restoration.restore(as_auth(lawyer), document_id)

    assert result.restored is True
    assert result.previous_hash is None
    assert result.preserved_copy_id is None
    assert result.verification.status == "AUTHENTIC"


async def test_altered_snapshot_with_a_bad_archive_is_refused(
    session, restoration, queries, tampered, backup, storage, lawyer
):
    document_id, url, _ = tampered
    await _alter_snapshot(session, document_id, file_sha256="0" * 64)
    [key] = [k for k in backup.objects if k.endswith(".pdf")]
    backup.objects[key] = storage.objects[url]
    objects = dict(storage.objects)

    with pytest.raises(ConflictError) as refused:
        await restoration.restore(as_auth(lawyer), document_id)

    assert refused.value.details["reason"] == "BACKUP_MISMATCH"
    await _assert_untouched(queries, lawyer, document_id, storage, objects)


async def test_archive_forged_to_match_an_altered_snapshot_is_never_used(
    session, restoration, queries, tampered, backup, storage, lawyer
):
    document_id, _, _ = tampered
    forged = b"forged"
    forged_hash = hashlib.sha256(forged).hexdigest()
    forged_key = f"{document_id}/{forged_hash}.pdf"
    backup.objects.clear()
    backup.objects[forged_key] = forged
    await _alter_snapshot(
        session,
        document_id,
        file_sha256=forged_hash,
        size_bytes=len(forged),
        backup_key=forged_key,
    )
    objects = dict(storage.objects)

    with pytest.raises(ConflictError) as refused:
        await restoration.restore(as_auth(lawyer), document_id)

    assert refused.value.details["reason"] == "NO_BACKUP"
    await _assert_untouched(queries, lawyer, document_id, storage, objects)


async def test_missing_archive_is_refused(
    restoration, queries, tampered, backup, storage, lawyer
):
    document_id, _, _ = tampered
    backup.objects.clear()
    objects = dict(storage.objects)

    with pytest.raises(ConflictError):
        await restoration.restore(as_auth(lawyer), document_id)

    await _assert_untouched(queries, lawyer, document_id, storage, objects)


async def test_unreachable_archive_is_refused(
    restoration, queries, tampered, backup, storage, lawyer
):
    document_id, _, _ = tampered
    objects = dict(storage.objects)

    async def down(_key):
        raise OSError("archive unreachable")

    backup.fetch = down

    with pytest.raises(ServiceUnavailableError):
        await restoration.restore(as_auth(lawyer), document_id)

    await _assert_untouched(queries, lawyer, document_id, storage, objects)


async def test_nulled_backup_key_does_not_block_restore(
    session, restoration, tampered, lawyer
):
    document_id, _, anchored = tampered
    await _alter_snapshot(session, document_id, backup_key=None)

    result = await restoration.restore(as_auth(lawyer), document_id)

    assert result.restored is True
    assert result.restored_hash == anchored
    assert result.verification.status == "AUTHENTIC"


async def test_backups_disabled_is_refused(
    session, chain, queries, tampered, storage, lawyer
):
    document_id, _, _ = tampered
    objects = dict(storage.objects)

    with pytest.raises(ConflictError):
        await DocumentRestoration(session, chain, None).restore(as_auth(lawyer), document_id)

    await _assert_untouched(queries, lawyer, document_id, storage, objects)


async def test_corrupted_archive_is_refused(
    restoration, queries, tampered, backup, storage, lawyer
):
    document_id, url, _ = tampered
    [key] = [k for k in backup.objects if k.endswith(".pdf")]
    backup.objects[key] = storage.objects[url]
    objects = dict(storage.objects)

    with pytest.raises(ConflictError):
        await restoration.restore(as_auth(lawyer), document_id)

    await _assert_untouched(queries, lawyer, document_id, storage, objects)


async def test_unreadable_current_file_is_refused(
    restoration, queries, tampered, storage, lawyer
):
    document_id, _, _ = tampered
    objects = dict(storage.objects)

    async def down(_url):
        raise OSError("storage unreachable")

    storage.download = down

    with pytest.raises(ServiceUnavailableError):
        await restoration.restore(as_auth(lawyer), document_id)

    del storage.download
    await _assert_untouched(queries, lawyer, document_id, storage, objects)


async def test_storage_failure_while_writing_rolls_back(
    restoration, queries, tampered, storage, lawyer
):
    document_id, _, _ = tampered
    objects = dict(storage.objects)
    real_upload = storage.upload_new
    calls = 0

    async def flaky(file_path, file_bytes, content_type="application/pdf"):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("storage unreachable")
        return await real_upload(file_path, file_bytes, content_type)

    storage.upload_new = flaky
    me = as_auth(lawyer)

    with pytest.raises(ServiceUnavailableError):
        await restoration.restore(me, document_id)

    # The rollback expired the session's objects; read the copies afresh.
    copies = (await queries.get_signed_copies(me, document_id)).copies
    assert len(copies) == 1 and copies[0].is_current
    assert storage.objects == objects


async def test_not_finalized_document_is_refused(
    restoration, intake, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("draft"), "Draft", open_book.id)

    with pytest.raises(ConflictError):
        await restoration.restore(me, doc.document_id)


# === Authorization ===


async def test_party_cannot_restore(session, restoration, tampered, backup, client):
    document_id, _, _ = tampered
    await add_party(session, document_id, client)
    fetched: list = []
    real_fetch = backup.fetch

    async def spy(key):
        fetched.append(key)
        return await real_fetch(key)

    backup.fetch = spy

    with pytest.raises(ForbiddenError):
        await restoration.restore(as_auth(client), document_id)
    assert fetched == []


async def test_other_lawyer_cannot_see_it(restoration, tampered, other_lawyer):
    document_id, _, _ = tampered

    with pytest.raises(NotFoundError):
        await restoration.restore(as_auth(other_lawyer), document_id)


# === Route ===


async def test_restore_route_is_lawyer_only_and_documented(
    session, tampered, client
):
    from app.infrastructure.database import get_db
    from app.infrastructure.security.dependencies import get_current_user
    from app.main import app

    document_id, _, _ = tampered

    async def same_session():
        yield session

    app.dependency_overrides[get_db] = same_session
    app.dependency_overrides[get_current_user] = lambda: as_auth(client)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as http:
            r = await http.post(f"/documents/{document_id}/restore")
            spec = (await http.get("/openapi.json")).json()
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 403
    operation = spec["paths"]["/documents/{document_id}/restore"]["post"]
    assert operation["responses"]["200"]


# === Deleted file ===


@pytest.fixture
async def deleted(finalized, storage):
    document_id, url, anchored = finalized
    del storage.objects[url]
    return document_id, url, anchored


async def test_restore_brings_back_a_deleted_file(
    restoration, verifier, deleted, lawyer
):
    document_id, _, anchored = deleted

    result = await restoration.restore(as_auth(lawyer), document_id)

    assert result.restored is True
    assert result.previous_hash is None
    assert result.preserved_copy_id is None
    assert result.restored_hash == anchored
    assert result.verification.status == "AUTHENTIC"
    after = await verifier.verify_document(as_auth(lawyer), document_id)
    assert after.status == "AUTHENTIC"
    assert after.current_hash == anchored


async def test_restoring_a_deleted_file_is_recorded_without_a_from_hash(
    restoration, queries, deleted, lawyer
):
    document_id, _, anchored = deleted

    await restoration.restore(as_auth(lawyer), document_id)

    last = (await queries.get_history(as_auth(lawyer), document_id)).changes[-1]
    assert last.action == "restore"
    assert (last.from_hash, last.to_hash) == (None, anchored)


async def test_deleted_file_with_no_archive_is_refused(
    restoration, deleted, backup, lawyer
):
    document_id, _, _ = deleted
    backup.objects.clear()

    with pytest.raises(ConflictError):
        await restoration.restore(as_auth(lawyer), document_id)


async def test_finalized_document_with_no_signed_copy_is_refused(
    session, restoration, finalized, lawyer
):
    document_id, _, _ = finalized

    document = await session.get(Document, document_id)
    document.signed_copy_id = None
    await session.flush()

    with pytest.raises(ConflictError):
        await restoration.restore(as_auth(lawyer), document_id)


async def test_edited_lifecycle_no_longer_blocks_restore(
    session, restoration, tampered, lawyer
):
    document_id, _, _ = tampered

    document = await session.get(Document, document_id)
    document.lifecycle = "SIGNED"
    await session.flush()

    result = await restoration.restore(as_auth(lawyer), document_id)

    assert result.restored is True
    assert document.lifecycle == "FINALIZED"
