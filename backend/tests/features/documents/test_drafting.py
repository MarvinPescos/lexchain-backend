"""The working draft in Google Docs: create, share, lock, unlock."""

import pytest
from sqlalchemy import select

from app.features.documents.models import DocumentParty, DraftRevocation
from app.shared.constants import (
    PARTY_ROLE_EDITOR,
    PARTY_STATUS_ACCEPTED,
    PARTY_STATUS_PENDING,
)
from app.shared.errors.exceptions import BadRequestError, ConflictError
from tests.conftest import as_auth, connect_google


async def add_party(session, document_id, user, status=PARTY_STATUS_ACCEPTED, role="viewer"):
    party = DocumentParty(
        document_id=document_id, user_id=user.id, email=user.email, status=status, role=role
    )
    session.add(party)
    await session.flush()
    return party


@pytest.fixture
async def doc(intake, google, lawyer):
    await connect_google(google, lawyer)
    return await intake.open_document(as_auth(lawyer), "Deed of Sale – Cruz lot")


async def test_blank_draft_is_shared_with_accepted_clients_only(
    drafting, drive, session, doc, lawyer, client, other_lawyer
):
    await add_party(session, doc.document_id, client)
    await add_party(session, doc.document_id, other_lawyer, status=PARTY_STATUS_PENDING)

    created = await drafting.create_draft(as_auth(lawyer), doc.document_id)

    file_id = created.draft_url.split("/d/")[1].split("/")[0]
    assert drive.roles(file_id) == {client.email: "commenter"}


async def test_editor_party_can_edit_the_draft(drafting, drive, session, doc, lawyer, other_lawyer):
    await add_party(session, doc.document_id, other_lawyer, role=PARTY_ROLE_EDITOR)
    created = await drafting.create_draft(as_auth(lawyer), doc.document_id)
    file_id = created.draft_url.split("/d/")[1].split("/")[0]
    assert drive.roles(file_id) == {other_lawyer.email: "writer"}


async def test_draft_from_template_and_existing_doc(drafting, drive, intake, lawyer, doc):
    me = as_auth(lawyer)
    template = drive.add_existing("Deed of Sale template")
    from_template = await drafting.create_draft(me, doc.document_id, "template", template)
    assert template not in from_template.draft_url  # a copy, not the template

    other = await intake.open_document(me, "Affidavit – Santos")
    started = drive.add_existing("Santos affidavit (started)")
    existing = await drafting.create_draft(me, other.document_id, "existing", started)
    assert started in existing.draft_url


async def test_existing_file_must_be_a_google_doc(drafting, drive, lawyer, doc):
    sheet = drive.add_existing("Fees", kind="spreadsheet")
    with pytest.raises(BadRequestError, match="must be a Google Doc"):
        await drafting.create_draft(as_auth(lawyer), doc.document_id, "existing", sheet)


async def test_template_needs_a_picked_file(drafting, lawyer, doc):
    with pytest.raises(BadRequestError, match="Pick the Google Doc"):
        await drafting.create_draft(as_auth(lawyer), doc.document_id, "template")


async def test_one_draft_per_document(drafting, lawyer, doc):
    await drafting.create_draft(as_auth(lawyer), doc.document_id)
    with pytest.raises(ConflictError, match="already has a working draft"):
        await drafting.create_draft(as_auth(lawyer), doc.document_id)


async def test_draft_needs_google_connected(intake, drafting, other_lawyer):
    me = as_auth(other_lawyer)
    doc = await intake.open_document(me, "Deed")
    with pytest.raises(ConflictError) as refused:
        await drafting.create_draft(me, doc.document_id)
    assert refused.value.details == {"reason": "GOOGLE_NOT_CONNECTED"}


async def test_no_draft_once_ready(drafting, lawyer, doc):
    await drafting.mark_ready(as_auth(lawyer), doc.document_id)
    with pytest.raises(ConflictError, match="Reopen the document"):
        await drafting.create_draft(as_auth(lawyer), doc.document_id)


async def test_ready_locks_and_reopen_unlocks(drafting, drive, session, doc, lawyer, client):
    me = as_auth(lawyer)
    await add_party(session, doc.document_id, client)
    created = await drafting.create_draft(me, doc.document_id)
    file_id = created.draft_url.split("/d/")[1].split("/")[0]

    await drafting.mark_ready(me, doc.document_id)
    assert drive.roles(file_id) == {client.email: "reader"}

    await drafting.reopen(me, doc.document_id)
    assert drive.roles(file_id) == {client.email: "commenter"}


async def test_accepting_after_the_draft_exists_shares_it(
    sharing, drafting, drive, session, doc, lawyer, client
):
    created = await drafting.create_draft(as_auth(lawyer), doc.document_id)
    file_id = created.draft_url.split("/d/")[1].split("/")[0]
    party = await add_party(session, doc.document_id, client, status=PARTY_STATUS_PENDING)
    assert drive.roles(file_id) == {}

    await sharing.respond_to_invitation(as_auth(client), party.id, accept=True)
    assert drive.roles(file_id) == {client.email: "commenter"}


async def test_accepting_while_ready_shares_read_only(
    sharing, drafting, drive, session, doc, lawyer, client
):
    me = as_auth(lawyer)
    created = await drafting.create_draft(me, doc.document_id)
    file_id = created.draft_url.split("/d/")[1].split("/")[0]
    await drafting.mark_ready(me, doc.document_id)
    party = await add_party(session, doc.document_id, client, status=PARTY_STATUS_PENDING)

    await sharing.respond_to_invitation(as_auth(client), party.id, accept=True)
    assert drive.roles(file_id) == {client.email: "reader"}


async def test_removing_a_party_unshares_the_draft(
    sharing, drafting, drive, session, doc, lawyer, client
):
    await add_party(session, doc.document_id, client)
    created = await drafting.create_draft(as_auth(lawyer), doc.document_id)
    file_id = created.draft_url.split("/d/")[1].split("/")[0]

    await sharing.remove_party(as_auth(lawyer), doc.document_id, client.id)
    assert drive.roles(file_id) == {}


async def test_google_down_does_not_block_accepting(
    sharing, drafting, drive, session, doc, lawyer, client, monkeypatch
):
    await drafting.create_draft(as_auth(lawyer), doc.document_id)
    party = await add_party(session, doc.document_id, client, status=PARTY_STATUS_PENDING)

    async def down(*args, **kwargs):
        raise RuntimeError("Drive unreachable")

    monkeypatch.setattr(drive, "share", down)
    result = await sharing.respond_to_invitation(as_auth(client), party.id, accept=True)
    assert result.status == PARTY_STATUS_ACCEPTED

    row = (await session.execute(select(DocumentParty).where(DocumentParty.id == party.id))).scalar_one()
    assert row.draft_permission_id is None


async def _revocations(session) -> list[DraftRevocation]:
    return list((await session.execute(select(DraftRevocation))).scalars().all())


async def test_google_down_keeps_the_revocation_for_retry(
    sharing, drafting, drive, session, doc, lawyer, client, monkeypatch
):
    await add_party(session, doc.document_id, client)
    created = await drafting.create_draft(as_auth(lawyer), doc.document_id)
    file_id = created.draft_url.split("/d/")[1].split("/")[0]
    unshare = drive.unshare

    async def down(*args, **kwargs):
        raise RuntimeError("Drive unreachable")

    monkeypatch.setattr(drive, "unshare", down)
    await sharing.remove_party(as_auth(lawyer), doc.document_id, client.id)

    # LexChain access is gone at once; the Drive permission is still there.
    parties = (await session.execute(select(DocumentParty))).scalars().all()
    assert parties == []
    assert drive.roles(file_id) == {client.email: "commenter"}
    [pending] = await _revocations(session)
    assert (pending.draft_file_id, pending.attempts) == (file_id, 1)
    assert "Drive unreachable" in pending.last_error

    # Still down: the retry keeps it.
    assert await drafting.revoke(pending) is False
    assert pending.attempts == 2

    # Back up: the retry removes the permission and forgets the revocation.
    monkeypatch.setattr(drive, "unshare", unshare)
    assert await drafting.revoke(pending) is True
    assert drive.roles(file_id) == {}
    assert await _revocations(session) == []


async def test_revoking_an_already_missing_permission_succeeds(
    sharing, drafting, drive, session, doc, lawyer, client, monkeypatch
):
    await add_party(session, doc.document_id, client)
    created = await drafting.create_draft(as_auth(lawyer), doc.document_id)
    file_id = created.draft_url.split("/d/")[1].split("/")[0]
    unshare = drive.unshare

    async def down(*args, **kwargs):
        raise RuntimeError("Drive unreachable")

    monkeypatch.setattr(drive, "unshare", down)
    await sharing.remove_party(as_auth(lawyer), doc.document_id, client.id)
    [pending] = await _revocations(session)

    # Someone removed the permission in Drive by hand meanwhile.
    drive.files[file_id]["permissions"].clear()
    monkeypatch.setattr(drive, "unshare", unshare)
    assert await drafting.revoke(pending) is True
    assert await _revocations(session) == []


async def test_party_added_back_cancels_their_pending_revocation(
    sharing, drafting, drive, session, doc, lawyer, client, monkeypatch
):
    await add_party(session, doc.document_id, client)
    created = await drafting.create_draft(as_auth(lawyer), doc.document_id)
    file_id = created.draft_url.split("/d/")[1].split("/")[0]
    [permission_id] = drive.files[file_id]["permissions"]
    share = drive.share

    async def down(*args, **kwargs):
        raise RuntimeError("Drive unreachable")

    monkeypatch.setattr(drive, "unshare", down)
    await sharing.remove_party(as_auth(lawyer), doc.document_id, client.id)
    assert len(await _revocations(session)) == 1

    # Drive hands the same person the same permission id again.
    async def same_id(*args, **kwargs):
        await share(*args, **kwargs)
        return permission_id

    monkeypatch.setattr(drive, "share", same_id)
    party = await add_party(session, doc.document_id, client, status=PARTY_STATUS_PENDING)
    await sharing.respond_to_invitation(as_auth(client), party.id, accept=True)
    assert await _revocations(session) == []
