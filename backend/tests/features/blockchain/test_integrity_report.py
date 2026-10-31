"""Verification history and the integrity report built from it.

Every check through GET /documents/{id}/verify is kept as its own row; the
report at GET /documents/verifications reads only those rows.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.features.blockchain.dependencies import get_blockchain_provider
from app.features.blockchain.models import VerificationResult
from app.features.documents.models import Document
from app.features.documents.workflow.finalization import DocumentFinalization
from app.features.snapshot.repository import DocumentSnaphotRepository
from app.infrastructure.database import get_db
from app.infrastructure.security.dependencies import get_current_user
from app.main import app
from tests.conftest import as_auth, pdf
from tests.features.documents.test_drafting import add_party

REPORT = "/documents/verifications"


@pytest.fixture
def http(session, chain):
    """Call the API as a given user, inside the test's transaction."""

    async def same_session():
        yield session

    async def call(user, path, **params):
        app.dependency_overrides[get_db] = same_session
        app.dependency_overrides[get_current_user] = lambda: as_auth(user)
        app.dependency_overrides[get_blockchain_provider] = lambda: chain
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t"
            ) as c:
                return await c.get(path, params=params)
        finally:
            app.dependency_overrides.clear()

    return call


@pytest.fixture
async def finalized(session, intake, queue, chain, storage, lawyer, open_book):
    """A finalized deed; returns (document_id, stored url)."""
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)
    await DocumentFinalization(session, queue, chain).finalize(me, doc.document_id)
    [url] = storage.objects
    return doc.document_id, url


async def _rows(session, document_id) -> list[VerificationResult]:
    result = await session.execute(
        select(VerificationResult)
        .where(VerificationResult.document_id == document_id)
        .order_by(VerificationResult.verified_at)
    )
    return list(result.scalars())


async def _result(session, document_id, status: str, verified_at: datetime, **fields):
    row = VerificationResult(
        document_id=document_id,
        status=status,
        is_authentic=status == "AUTHENTIC",
        baseline_trusted=fields.pop("baseline_trusted", status != "SNAPSHOT_COMPROMISED"),
        verified_at=verified_at,
        tamper_report_status=fields.pop("tamper_report_status", "NOT_APPLICABLE"),
        **fields,
    )
    session.add(row)
    await session.flush()
    return row


# === Recording ===


async def test_each_check_through_the_endpoint_is_kept_as_its_own_row(
    session, http, finalized, storage, lawyer
):
    document_id, url = finalized

    first = await http(lawyer, f"/documents/{document_id}/verify")
    storage.objects[url] += b" tampered"
    second = await http(lawyer, f"/documents/{document_id}/verify")

    assert first.status_code == second.status_code == 200
    rows = await _rows(session, document_id)
    assert [r.status for r in rows] == ["AUTHENTIC", "TAMPERED"]
    assert rows[0].verified_at < rows[1].verified_at
    assert rows[0].verified_at == datetime.fromisoformat(first.json()["verified_at"])
    assert rows[1].current_hash == second.json()["current_hash"]
    assert rows[1].onchain_hash == second.json()["onchain_hash"]
    assert all(r.verified_by == lawyer.id for r in rows)
    assert rows[1].baseline_trusted is True


async def test_the_verify_response_is_unchanged(http, finalized, lawyer):
    document_id, _ = finalized

    body = (await http(lawyer, f"/documents/{document_id}/verify")).json()

    assert body["status"] == "AUTHENTIC"
    assert body["is_authentic"] is True
    assert "id" not in body


async def test_a_check_that_could_not_read_the_file_is_kept_as_unavailable(
    session, verification, finalized, storage, lawyer
):
    document_id, url = finalized
    del storage.objects[url]
    storage.download = _raise_os_error  # type: ignore[method-assign]

    await verification.verify_document(as_auth(lawyer), document_id, record=True)

    [row] = await _rows(session, document_id)
    assert row.status == "VERIFICATION_UNAVAILABLE"
    assert row.is_authentic is False
    assert row.current_hash is None


async def _raise_os_error(url: str) -> bytes:
    raise OSError("storage down")


async def test_a_compromised_baseline_is_kept_as_such(
    session, verification, finalized, storage, lawyer
):
    document_id, url = finalized
    snapshot = await DocumentSnaphotRepository(session).get_by_document_id(document_id)
    snapshot.file_sha256 = "0" * 64
    storage.objects[url] += b" tampered"

    await verification.verify_document(as_auth(lawyer), document_id, record=True)

    [row] = await _rows(session, document_id)
    assert row.status == "SNAPSHOT_COMPROMISED"
    assert row.baseline_trusted is False
    assert row.snapshot_hash == "0" * 64


async def test_a_document_never_finalized_is_kept_as_not_anchored(
    session, verification, intake, lawyer, open_book
):
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, pdf("deed"), "Deed", open_book.id)

    await verification.verify_document(me, doc.document_id, record=True)

    [row] = await _rows(session, doc.document_id)
    assert row.status == "NOT_ANCHORED"
    assert row.onchain_hash is None


async def test_internal_checks_are_not_recorded_unless_asked(
    session, verification, finalized, lawyer
):
    document_id, _ = finalized

    await verification.verify_document(as_auth(lawyer), document_id)

    assert await _rows(session, document_id) == []


# === Report ===


T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


async def _document(session, owner, name="Deed") -> Document:
    document = Document(user_id=owner.id, file_name=name, lifecycle="FINALIZED")
    session.add(document)
    await session.flush()
    return document


async def test_report_lists_every_status_distinctly_with_matching_totals(
    session, http, lawyer
):
    document = await _document(session, lawyer)
    statuses = [
        "AUTHENTIC",
        "AUTHENTIC",
        "TAMPERED",
        "SNAPSHOT_COMPROMISED",
        "NOT_ANCHORED",
        "VERIFICATION_UNAVAILABLE",
    ]
    for i, s in enumerate(statuses):
        await _result(session, document.id, s, T0 + timedelta(hours=i))

    r = await http(lawyer, REPORT)

    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == {
        "total": 6,
        "authentic": 2,
        "tampered": 1,
        "snapshot_compromised": 1,
        "not_anchored": 1,
        "verification_unavailable": 1,
    }
    assert [x["status"] for x in body["results"]] == list(reversed(statuses))
    unavailable = body["results"][0]
    assert unavailable["is_authentic"] is False
    assert unavailable["file_name"] == "Deed"
    assert unavailable["document_id"] == str(document.id)


async def test_report_range_is_from_inclusive_to_exclusive_on_verified_at(
    session, http, lawyer
):
    document = await _document(session, lawyer)
    before = await _result(session, document.id, "AUTHENTIC", T0 - timedelta(microseconds=1))
    at_start = await _result(session, document.id, "TAMPERED", T0)
    inside = await _result(session, document.id, "AUTHENTIC", T0 + timedelta(days=1))
    at_end = await _result(session, document.id, "AUTHENTIC", T0 + timedelta(days=2))

    r = await http(
        lawyer,
        REPORT,
        verified_from=T0.isoformat(),
        verified_to=(T0 + timedelta(days=2)).isoformat(),
    )

    body = r.json()
    ids = [x["id"] for x in body["results"]]
    assert ids == [str(inside.id), str(at_start.id)]
    assert str(before.id) not in ids and str(at_end.id) not in ids
    assert body["summary"]["total"] == 2
    assert body["summary"]["tampered"] == 1


async def test_report_reads_a_time_without_offset_as_utc(session, http, lawyer):
    document = await _document(session, lawyer)
    await _result(session, document.id, "AUTHENTIC", T0)

    r = await http(lawyer, REPORT, verified_from="2026-10-01T00:00:00")

    assert r.json()["summary"]["total"] == 1
    assert r.json()["verified_from"] == "2026-10-01T00:00:00Z"


async def test_report_rejects_an_empty_range(http, lawyer):
    r = await http(
        lawyer,
        REPORT,
        verified_from=T0.isoformat(),
        verified_to=T0.isoformat(),
    )

    assert r.status_code == 400


async def test_report_pages_stably_with_totals_for_the_whole_range(
    session, http, lawyer
):
    document = await _document(session, lawyer)
    # Two rows share a timestamp: the id breaks the tie so pages never overlap.
    rows = [await _result(session, document.id, "AUTHENTIC", T0) for _ in range(2)]
    rows += [
        await _result(session, document.id, "TAMPERED", T0 + timedelta(hours=i))
        for i in (1, 2, 3)
    ]

    pages = [
        (await http(lawyer, REPORT, limit=2, offset=offset)).json()
        for offset in (0, 2, 4)
    ]

    seen = [x["id"] for page in pages for x in page["results"]]
    assert len(seen) == len(set(seen)) == 5
    assert {str(r.id) for r in rows} == set(seen)
    assert [len(p["results"]) for p in pages] == [2, 2, 1]
    assert all(p["summary"]["total"] == 5 for p in pages)
    assert all(p["summary"]["tampered"] == 3 for p in pages)
    assert pages[1]["limit"] == 2 and pages[1]["offset"] == 2


async def test_report_shows_only_the_lawyers_own_documents(
    session, http, lawyer, other_lawyer
):
    mine = await _document(session, lawyer, "Mine")
    theirs = await _document(session, other_lawyer, "Theirs")
    await _result(session, mine.id, "AUTHENTIC", T0)
    await _result(session, theirs.id, "TAMPERED", T0)

    body = (await http(lawyer, REPORT)).json()

    assert [x["file_name"] for x in body["results"]] == ["Mine"]
    assert body["summary"]["total"] == 1
    assert body["summary"]["tampered"] == 0


async def test_a_lawyer_who_is_a_party_does_not_see_the_owners_report(
    session, http, lawyer, other_lawyer
):
    document = await _document(session, lawyer)
    await add_party(session, document.id, other_lawyer)
    await _result(session, document.id, "AUTHENTIC", T0)

    body = (await http(other_lawyer, REPORT)).json()

    assert body["results"] == []
    assert body["summary"]["total"] == 0


async def test_a_client_party_cannot_read_the_report(session, http, lawyer, client):
    document = await _document(session, lawyer)
    await add_party(session, document.id, client)
    await _result(session, document.id, "AUTHENTIC", T0)

    r = await http(client, REPORT)

    assert r.status_code == 403


async def test_report_does_not_touch_storage_or_the_chain(
    session, http, finalized, storage, chain, lawyer
):
    document_id, _ = finalized
    await http(lawyer, f"/documents/{document_id}/verify")

    async def boom(*args, **kwargs):
        raise AssertionError("the report must not re-verify")

    storage.download = boom  # type: ignore[method-assign]
    chain.get_document_hash = boom  # type: ignore[method-assign]

    r = await http(lawyer, REPORT)

    assert r.status_code == 200
    assert r.json()["summary"]["authentic"] == 1


def test_report_contract_is_in_openapi():
    schema = app.openapi()
    operation = schema["paths"][REPORT]["get"]
    params = {p["name"] for p in operation["parameters"]}
    assert {"verified_from", "verified_to", "limit", "offset"} <= params
    assert "IntegrityReportResponse" in schema["components"]["schemas"]


async def test_a_failed_history_write_still_returns_the_verdict(
    session, verification, finalized, lawyer, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    document_id, _ = finalized

    async def broken_commit():
        raise OperationalError("INSERT", {}, Exception("db down"))

    monkeypatch.setattr(session, "commit", broken_commit)

    result = await verification.verify_document(as_auth(lawyer), document_id, record=True)

    assert result.status == "AUTHENTIC"


async def test_the_report_requires_a_login(session):
    async def same_session():
        yield session

    app.dependency_overrides[get_db] = same_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            r = await c.get(REPORT)
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 401
