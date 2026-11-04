"""E-copy requests: a party asks for the signed original, the owner decides,
and until then the party only ever reads a watermarked preview."""

import hashlib
import io
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.features.documents.constants import (
    DELIVERY_ERROR_ACCESS,
    DELIVERY_ERROR_EMAIL_UNVERIFIED,
    DELIVERY_ERROR_INTEGRITY,
    DELIVERY_ERROR_PROVIDER,
    DELIVERY_ERROR_TOO_LARGE,
    DELIVERY_FAILED,
    DELIVERY_SENDING,
    DELIVERY_SENT,
    ECOPY_APPROVED,
    ECOPY_DECLINED,
    ECOPY_PENDING,
)
from app.features.documents.dependencies import get_ecopy
from app.features.documents.models import Document, EcopyRequest, SignedCopy
from app.features.documents.workflow.ecopy import DocumentEcopy
from app.features.notifications.models import Notification, NotificationType
from app.infrastructure.database import get_db
from app.infrastructure.email import EmailMessage
from app.infrastructure.security.dependencies import get_current_user
from app.main import app
from app.shared.constants import PARTY_STATUS_PENDING
from app.shared.errors.exceptions import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
)
from tests.conftest import _make_user, as_auth, pdf, real_pdf, mark_processed
from tests.features.documents.test_drafting import add_party


class FakeEmail:
    """Records what would be sent; `fail` / `boom` make the next sends fail."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.fail = False
        self.boom = False

    async def send(self, message: EmailMessage) -> bool:
        if self.boom:
            raise RuntimeError("provider exploded")
        if self.fail:
            return False
        self.sent.append(message)
        return True


@pytest.fixture
def email() -> FakeEmail:
    return FakeEmail()


@pytest.fixture
def ecopy(session, storage, email) -> DocumentEcopy:
    return DocumentEcopy(session, email)


@pytest.fixture
async def finalized(session, intake, finalization, storage, lawyer, client, open_book):
    """A finalized deed with `client` on it; returns (document_id, original bytes)."""
    me = as_auth(lawyer)
    doc = await intake.upload_signed(me, real_pdf("deed"), "Deed of Sale", open_book.id)
    await mark_processed(session, doc.document_id)
    await finalization.finalize(me, doc.document_id)
    await add_party(session, doc.document_id, client)
    [original] = storage.objects.values()
    return doc.document_id, original


async def notifications(session, user, type):
    result = await session.execute(
        select(Notification).where(Notification.user_id == user.id, Notification.type == type)
    )
    return list(result.scalars().all())


# === Requesting ===


async def test_client_requests_a_finalized_document_and_the_lawyer_is_notified(
    ecopy, session, finalized, lawyer, client
):
    document_id, _ = finalized

    r = await ecopy.request(as_auth(client), document_id)

    assert (r.status, r.delivery_status, r.client_id) == (ECOPY_PENDING, None, client.id)
    assert [e.action for e in r.events] == ["requested"]
    [note] = await notifications(session, lawyer, NotificationType.ECOPY_REQUESTED)
    assert note.event_metadata == {"document_id": str(document_id), "request_id": str(r.id)}


async def test_only_one_pending_request_per_client_and_document(ecopy, finalized, client):
    document_id, _ = finalized
    await ecopy.request(as_auth(client), document_id)

    with pytest.raises(ConflictError, match="pending"):
        await ecopy.request(as_auth(client), document_id)


async def test_the_database_refuses_a_second_pending_request(session, finalized, client):
    document_id, _ = finalized
    session.add(EcopyRequest(document_id=document_id, client_id=client.id))
    await session.flush()

    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(EcopyRequest(document_id=document_id, client_id=client.id))
            await session.flush()


async def test_who_may_request(ecopy, session, intake, finalized, lawyer, other_lawyer, open_book):
    document_id, _ = finalized

    with pytest.raises(BadRequestError):
        await ecopy.request(as_auth(lawyer), document_id)
    with pytest.raises(NotFoundError):
        await ecopy.request(as_auth(other_lawyer), document_id)

    invited = await _make_user(session, "user", "invited")
    await add_party(session, document_id, invited, status=PARTY_STATUS_PENDING)
    with pytest.raises(NotFoundError):
        await ecopy.request(as_auth(invited), document_id)


async def test_a_document_not_yet_finalized_cannot_be_requested(
    ecopy, session, intake, lawyer, client, open_book
):
    doc = await intake.upload_signed(as_auth(lawyer), real_pdf("signed"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await add_party(session, doc.document_id, client)

    with pytest.raises(ConflictError, match="finalized"):
        await ecopy.request(as_auth(client), doc.document_id)


# === Watermark boundary ===


async def test_before_approval_the_client_reads_only_a_watermarked_preview(
    queries, ecopy, finalized, storage, lawyer, client
):
    document_id, original = finalized
    await ecopy.request(as_auth(client), document_id)

    preview = await queries.get_signed_copy_file(as_auth(client), document_id)

    assert preview.watermarked is True
    assert "/previews/" in preview.url
    preview_bytes = storage.objects[preview.url.split("?")[0]]
    assert preview_bytes != original
    reader = PdfReader(io.BytesIO(preview_bytes))
    assert reader.metadata.title != "deed"  # nothing of the original's PDF carried over
    assert len(reader.pages) == 1

    own = await queries.get_signed_copy_file(as_auth(lawyer), document_id)
    assert own.watermarked is False
    assert storage.objects[own.url.split("?")[0]] == original


async def test_older_copies_are_watermarked_for_a_party_too(
    queries, session, intake, finalization, storage, lawyer, client, open_book
):
    me = as_auth(lawyer)
    first = await intake.upload_signed(me, real_pdf("scan-1"), "Deed", open_book.id)
    await intake.replace_signed_copy(me, first.document_id, real_pdf("scan-2"), "Wrong scan")
    await add_party(session, first.document_id, client)

    older = await queries.get_signed_copy_file(
        as_auth(client), first.document_id, first.signed_copy.id
    )

    assert older.watermarked is True and "/previews/" in older.url


async def test_preview_is_made_once_per_reader_and_copy(queries, finalized, storage, client):
    document_id, _ = finalized

    a = await queries.get_signed_copy_file(as_auth(client), document_id)
    count = len(storage.objects)
    b = await queries.get_signed_copy_file(as_auth(client), document_id)

    assert a.url == b.url and len(storage.objects) == count


async def test_a_file_that_cannot_be_rendered_is_refused_not_served(
    queries, session, intake, lawyer, client, open_book
):
    doc = await intake.upload_signed(as_auth(lawyer), pdf("garbled"), "Deed", open_book.id)
    await mark_processed(session, doc.document_id)
    await add_party(session, doc.document_id, client)

    with pytest.raises(ServiceUnavailableError):
        await queries.get_signed_copy_file(as_auth(client), doc.document_id)


# === Approving ===


async def test_approval_emails_the_original_and_unlocks_it_in_the_app(
    ecopy, queries, session, finalized, storage, email, lawyer, client, verification
):
    document_id, original = finalized
    req = await ecopy.request(as_auth(client), document_id)

    r = await ecopy.approve(as_auth(lawyer), document_id, req.id)

    assert (r.status, r.delivery_status, r.delivered_to) == (
        ECOPY_APPROVED, DELIVERY_SENT, client.email
    )
    assert r.decided_by == lawyer.id and r.can_download and not r.can_retry_delivery
    assert [e.action for e in r.events] == [
        "requested", "approved", "delivery_attempted", "delivered"
    ]
    [message] = email.sent
    assert message.to == client.email
    [attachment] = message.attachments
    assert attachment.content == original and attachment.filename == "Deed of Sale.pdf"
    assert "http" not in message.text  # no links to the file

    file = await queries.get_signed_copy_file(as_auth(client), document_id)
    assert file.watermarked is False
    assert storage.objects[file.url.split("?")[0]] == original
    assert await notifications(session, client, NotificationType.ECOPY_APPROVED)

    # The original and its fingerprint are untouched.
    after = await verification.verify_document(as_auth(lawyer), document_id)
    assert after.status == "AUTHENTIC"
    assert after.current_hash == hashlib.sha256(original).hexdigest()


async def test_only_the_owner_decides(ecopy, finalized, client, other_lawyer):
    document_id, _ = finalized
    req = await ecopy.request(as_auth(client), document_id)

    with pytest.raises(ForbiddenError):
        await ecopy.approve(as_auth(client), document_id, req.id)
    with pytest.raises(NotFoundError):
        await ecopy.approve(as_auth(other_lawyer), document_id, req.id)
    with pytest.raises(NotFoundError):
        await ecopy.decline(as_auth(other_lawyer), document_id, req.id)


async def test_a_request_is_decided_once(ecopy, finalized, email, lawyer, client):
    document_id, _ = finalized
    req = await ecopy.request(as_auth(client), document_id)
    await ecopy.approve(as_auth(lawyer), document_id, req.id)

    with pytest.raises(ConflictError, match="already approved"):
        await ecopy.approve(as_auth(lawyer), document_id, req.id)
    with pytest.raises(ConflictError, match="already approved"):
        await ecopy.decline(as_auth(lawyer), document_id, req.id)
    assert len(email.sent) == 1


async def test_an_approved_client_cannot_request_the_same_copy_again(
    ecopy, finalized, lawyer, client
):
    document_id, _ = finalized
    req = await ecopy.request(as_auth(client), document_id)
    await ecopy.approve(as_auth(lawyer), document_id, req.id)

    with pytest.raises(ConflictError, match="already approved"):
        await ecopy.request(as_auth(client), document_id)


async def test_approval_covers_only_the_requesting_client(
    ecopy, queries, session, finalized, lawyer, client
):
    document_id, _ = finalized
    other_client = await _make_user(session, "user", "other-client")
    await add_party(session, document_id, other_client)
    req = await ecopy.request(as_auth(client), document_id)
    await ecopy.approve(as_auth(lawyer), document_id, req.id)

    file = await queries.get_signed_copy_file(as_auth(other_client), document_id)

    assert file.watermarked is True


# === Declining ===


async def test_decline_notifies_with_the_reason_and_the_client_may_ask_again(
    ecopy, queries, session, finalized, email, lawyer, client
):
    document_id, _ = finalized
    req = await ecopy.request(as_auth(client), document_id)

    r = await ecopy.decline(as_auth(lawyer), document_id, req.id, "  Settle the fee first.  ")

    assert (r.status, r.decline_reason, r.delivery_status) == (
        ECOPY_DECLINED, "Settle the fee first.", None
    )
    [note] = await notifications(session, client, NotificationType.ECOPY_DECLINED)
    assert "Settle the fee first." in note.body
    assert email.sent == []
    assert (await queries.get_signed_copy_file(as_auth(client), document_id)).watermarked

    again = await ecopy.request(as_auth(client), document_id)
    assert again.status == ECOPY_PENDING and again.id != req.id


# === Delivery failures and retry ===


async def test_a_failed_email_stays_visibly_owed_and_can_be_retried(
    ecopy, queries, session, finalized, email, lawyer, client
):
    document_id, original = finalized
    req = await ecopy.request(as_auth(client), document_id)
    email.fail = True

    r = await ecopy.approve(as_auth(lawyer), document_id, req.id)

    assert (r.status, r.delivery_status, r.delivery_error) == (
        ECOPY_APPROVED, DELIVERY_FAILED, DELIVERY_ERROR_PROVIDER
    )
    assert r.delivered_at is None and r.delivered_to is None and r.can_retry_delivery
    assert await notifications(session, lawyer, NotificationType.ECOPY_DELIVERY_FAILED)
    # Approval stands: the copy is available in the app regardless.
    assert (await queries.get_signed_copy_file(as_auth(client), document_id)).watermarked is False

    email.fail = False
    retried = await ecopy.retry_delivery(as_auth(lawyer), document_id, req.id)

    assert (retried.delivery_status, retried.delivery_attempts) == (DELIVERY_SENT, 2)
    assert retried.delivery_error is None
    assert [a.content for m in email.sent for a in m.attachments] == [original]
    assert [e.action for e in retried.events] == [
        "requested", "approved", "delivery_attempted", "delivery_failed",
        "delivery_attempted", "delivered",
    ]


async def test_a_provider_exception_is_a_failed_send(ecopy, finalized, email, lawyer, client):
    document_id, _ = finalized
    req = await ecopy.request(as_auth(client), document_id)
    email.boom = True

    r = await ecopy.approve(as_auth(lawyer), document_id, req.id)

    assert (r.delivery_status, r.delivery_error) == (DELIVERY_FAILED, DELIVERY_ERROR_PROVIDER)
    detail = [e.detail for e in r.events if e.action == "delivery_failed"]
    assert detail == [DELIVERY_ERROR_PROVIDER]  # a code, never provider text


async def test_retry_is_refused_unless_a_send_is_owed(
    ecopy, session, finalized, email, lawyer, client
):
    document_id, _ = finalized
    req = await ecopy.request(as_auth(client), document_id)

    with pytest.raises(ConflictError, match="approved"):
        await ecopy.retry_delivery(as_auth(lawyer), document_id, req.id)

    await ecopy.approve(as_auth(lawyer), document_id, req.id)
    with pytest.raises(ConflictError, match="already delivered"):
        await ecopy.retry_delivery(as_auth(lawyer), document_id, req.id)
    with pytest.raises(ForbiddenError):
        await ecopy.retry_delivery(as_auth(client), document_id, req.id)
    assert len(email.sent) == 1


async def test_a_send_in_progress_is_not_doubled_until_it_goes_stale(
    ecopy, session, finalized, email, lawyer, client
):
    document_id, _ = finalized
    req = await ecopy.request(as_auth(client), document_id)
    email.fail = True
    await ecopy.approve(as_auth(lawyer), document_id, req.id)
    email.fail = False

    # As if another worker had just claimed the send.
    row = await session.get(EcopyRequest, req.id)
    row.delivery_status = DELIVERY_SENDING
    row.delivery_started_at = datetime.now(timezone.utc)
    await session.flush()
    with pytest.raises(ConflictError, match="in progress"):
        await ecopy.retry_delivery(as_auth(lawyer), document_id, req.id)
    assert email.sent == []

    # That worker died: after the stale window the send can be taken over.
    row.delivery_started_at = datetime.now(timezone.utc) - timedelta(minutes=11)
    await session.flush()
    r = await ecopy.retry_delivery(as_auth(lawyer), document_id, req.id)
    assert r.delivery_status == DELIVERY_SENT and len(email.sent) == 1


async def test_only_one_of_two_claims_wins(ecopy, session, finalized, email, lawyer, client):
    from app.features.documents.repository import DocumentRepository

    document_id, _ = finalized
    req = await ecopy.request(as_auth(client), document_id)
    email.fail = True
    await ecopy.approve(as_auth(lawyer), document_id, req.id)

    repo = DocumentRepository(session)
    assert await repo.claim_ecopy_delivery(req.id) is True
    assert await repo.claim_ecopy_delivery(req.id) is False


async def test_unverified_email_is_not_sent_to(ecopy, session, finalized, email, lawyer, client):
    document_id, _ = finalized
    req = await ecopy.request(as_auth(client), document_id)
    client.is_email_verified = False
    await session.flush()

    r = await ecopy.approve(as_auth(lawyer), document_id, req.id)

    assert (r.delivery_status, r.delivery_error) == (
        DELIVERY_FAILED, DELIVERY_ERROR_EMAIL_UNVERIFIED
    )
    assert email.sent == []


async def test_tampered_bytes_are_never_emailed(
    ecopy, finalized, storage, email, lawyer, client
):
    document_id, original = finalized
    req = await ecopy.request(as_auth(client), document_id)
    [url] = [u for u, b in storage.objects.items() if b == original]
    storage.objects[url] = original + b" tampered"

    r = await ecopy.approve(as_auth(lawyer), document_id, req.id)

    assert (r.delivery_status, r.delivery_error) == (DELIVERY_FAILED, DELIVERY_ERROR_INTEGRITY)
    assert email.sent == []


async def test_a_file_over_the_attachment_limit_is_not_sent(
    ecopy, session, finalized, email, lawyer, client
):
    document_id, _ = finalized
    req = await ecopy.request(as_auth(client), document_id)
    document = await session.get(Document, document_id)
    copy = await session.get(SignedCopy, document.signed_copy_id)
    copy.size_bytes = 30 * 1024 * 1024
    await session.flush()

    r = await ecopy.approve(as_auth(lawyer), document_id, req.id)

    assert (r.delivery_status, r.delivery_error) == (DELIVERY_FAILED, DELIVERY_ERROR_TOO_LARGE)
    assert email.sent == []


# === Access revocation and file changes ===


async def test_a_removed_client_is_neither_approved_nor_served(
    ecopy, queries, sharing, finalized, email, lawyer, client
):
    document_id, _ = finalized
    req = await ecopy.request(as_auth(client), document_id)
    await sharing.remove_party(as_auth(lawyer), document_id, client.id)

    with pytest.raises(ConflictError, match="no longer has access"):
        await ecopy.approve(as_auth(lawyer), document_id, req.id)
    with pytest.raises(NotFoundError):
        await queries.get_signed_copy_file(as_auth(client), document_id)
    with pytest.raises(NotFoundError):
        await ecopy.list_for_document(as_auth(client), document_id)
    assert email.sent == []


async def test_revoking_after_approval_stops_download_and_retry_sends(
    ecopy, queries, sharing, finalized, email, lawyer, client
):
    document_id, _ = finalized
    req = await ecopy.request(as_auth(client), document_id)
    email.fail = True
    await ecopy.approve(as_auth(lawyer), document_id, req.id)
    email.fail = False
    await sharing.remove_party(as_auth(lawyer), document_id, client.id)

    r = await ecopy.retry_delivery(as_auth(lawyer), document_id, req.id)

    assert (r.delivery_status, r.delivery_error) == (DELIVERY_FAILED, DELIVERY_ERROR_ACCESS)
    assert r.can_download is False
    assert email.sent == []
    with pytest.raises(NotFoundError):
        await queries.get_signed_copy_file(as_auth(client), document_id)


async def test_an_approval_does_not_cover_a_different_file(
    ecopy, queries, session, finalized, email, lawyer, client
):
    document_id, _ = finalized
    req = await ecopy.request(as_auth(client), document_id)
    email.fail = True
    await ecopy.approve(as_auth(lawyer), document_id, req.id)
    email.fail = False

    # The document's current file changes to other bytes.
    document = await session.get(Document, document_id)
    document.document_hash = "f" * 64
    copy = await session.get(SignedCopy, document.signed_copy_id)
    copy.sha256 = "f" * 64
    await session.flush()

    file = await queries.get_signed_copy_file(as_auth(client), document_id)
    assert file.watermarked is True
    r = await ecopy.retry_delivery(as_auth(lawyer), document_id, req.id)
    assert (r.delivery_error, r.can_download) == (DELIVERY_ERROR_INTEGRITY, False)
    assert email.sent == []


# === Listing ===


async def test_owner_sees_every_request_and_a_client_only_their_own(
    ecopy, session, finalized, lawyer, client, other_lawyer
):
    document_id, _ = finalized
    other_client = await _make_user(session, "user", "other-client")
    await add_party(session, document_id, other_client)
    mine = await ecopy.request(as_auth(client), document_id)
    theirs = await ecopy.request(as_auth(other_client), document_id)

    owner_view = await ecopy.list_for_document(as_auth(lawyer), document_id)
    client_view = await ecopy.list_for_document(as_auth(client), document_id)

    assert {r.id for r in owner_view.requests} == {mine.id, theirs.id}
    assert [r.id for r in client_view.requests] == [mine.id]
    assert [r.id for r in (await ecopy.list_mine(as_auth(client))).requests] == [mine.id]

    queue = await ecopy.list_for_owner(as_auth(lawyer), ECOPY_PENDING)
    assert {r.id for r in queue.requests} == {mine.id, theirs.id}
    assert (await ecopy.list_for_owner(as_auth(other_lawyer))).total == 0


async def test_permissions_offer_request_to_parties_and_review_to_the_owner(
    queries, finalized, lawyer, client
):
    document_id, _ = finalized

    as_client = (await queries.get_document(as_auth(client), document_id)).permissions
    as_owner = (await queries.get_document(as_auth(lawyer), document_id)).permissions

    assert (as_client.can_request_ecopy, as_client.can_review_ecopy) == (True, False)
    assert (as_owner.can_request_ecopy, as_owner.can_review_ecopy) == (False, True)


# === Routes ===


async def call(session, ecopy, user, method, path, **kwargs):
    async def same_session():
        yield session

    app.dependency_overrides[get_db] = same_session
    app.dependency_overrides[get_current_user] = lambda: as_auth(user)
    app.dependency_overrides[get_ecopy] = lambda: ecopy
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as http:
            return await http.request(method, path, **kwargs)
    finally:
        app.dependency_overrides.clear()


async def test_routes_request_review_and_refuse_clients_reviewing(
    ecopy, session, finalized, lawyer, client
):
    document_id, _ = finalized

    created = await call(session, ecopy, client, "POST", f"/documents/{document_id}/ecopy-requests")
    assert created.status_code == 201
    request_id = created.json()["id"]
    dup = await call(session, ecopy, client, "POST", f"/documents/{document_id}/ecopy-requests")
    assert dup.status_code == 409

    base = f"/documents/{document_id}/ecopy-requests/{request_id}"
    assert (await call(session, ecopy, client, "POST", f"{base}/approve")).status_code == 403

    queue = await call(session, ecopy, lawyer, "GET", "/documents/ecopy-requests?status=PENDING")
    assert [r["id"] for r in queue.json()["requests"]] == [request_id]

    declined = await call(
        session, ecopy, lawyer, "POST", f"{base}/decline", json={"reason": "Not yet"}
    )
    assert declined.status_code == 200
    assert (declined.json()["status"], declined.json()["decline_reason"]) == (
        "DECLINED", "Not yet"
    )
