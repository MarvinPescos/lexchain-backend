"""Inviting someone who has no LexChain account yet."""

from datetime import datetime, timedelta, timezone

import pytest

from app.shared.constants import (
    PARTY_ROLE_EDITOR,
    PARTY_STATUS_ACCEPTED,
    PARTY_STATUS_EXPIRED,
    PARTY_STATUS_PENDING,
)
from app.shared.errors.exceptions import BadRequestError, ConflictError, NotFoundError
from tests.conftest import _make_user, as_auth, connect_google

NEWCOMER = "Newcomer@Example.com"


@pytest.fixture
def invite_emails(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    import app.celery_tasks as tasks

    queued: list[dict] = []
    monkeypatch.setattr(
        tasks.send_document_invite_email, "delay", lambda **kw: queued.append(kw)
    )
    return queued


@pytest.fixture
async def doc(intake, google, lawyer):
    await connect_google(google, lawyer)
    return await intake.open_document(as_auth(lawyer), "Deed of Sale – Cruz lot")


async def _sign_up(session, email: str):
    user = await _make_user(session, "user", "newcomer")
    user.email = email
    await session.flush()
    return user


async def test_unknown_email_is_held_as_an_invitation_and_emailed(
    sharing, doc, lawyer, invite_emails
):
    party = await sharing.add_party(as_auth(lawyer), doc.document_id, NEWCOMER)

    assert party.user_id is None
    assert party.email == NEWCOMER.lower()
    assert party.status == PARTY_STATUS_PENDING
    assert party.invite_expires_at > datetime.now(timezone.utc) + timedelta(days=6)
    assert invite_emails == [
        {
            "email": NEWCOMER.lower(),
            "owner_name": f"{lawyer.f_name} {lawyer.l_name}",
            "document_name": "Deed of Sale – Cruz lot",
        }
    ]

    listed = await sharing.get_parties(as_auth(lawyer), doc.document_id)
    assert [(p.email, p.user_id, p.status) for p in listed.parties] == [
        (NEWCOMER.lower(), None, PARTY_STATUS_PENDING)
    ]


async def test_the_same_address_cannot_be_invited_twice(sharing, doc, lawyer, invite_emails):
    await sharing.add_party(as_auth(lawyer), doc.document_id, NEWCOMER)
    with pytest.raises(ConflictError):
        await sharing.add_party(as_auth(lawyer), doc.document_id, NEWCOMER.lower())


async def test_an_email_invitation_cannot_grant_a_lawyer_role(
    sharing, doc, lawyer, invite_emails
):
    with pytest.raises(BadRequestError):
        await sharing.add_party(
            as_auth(lawyer), doc.document_id, NEWCOMER, PARTY_ROLE_EDITOR
        )
    assert invite_emails == []


async def test_first_verified_sign_in_joins_the_document(
    sharing, drafting, drive, session, doc, lawyer, invite_emails
):
    created = await drafting.create_draft(as_auth(lawyer), doc.document_id)
    file_id = created.draft_url.split("/d/")[1].split("/")[0]
    await sharing.add_party(as_auth(lawyer), doc.document_id, NEWCOMER)

    newcomer = await _sign_up(session, NEWCOMER)
    assert await sharing.claim_email_invites(newcomer.id, newcomer.email) == 1

    listed = await sharing.get_parties(as_auth(lawyer), doc.document_id)
    [party] = listed.parties
    assert party.user_id == newcomer.id
    assert party.status == PARTY_STATUS_ACCEPTED
    assert drive.roles(file_id) == {NEWCOMER.lower(): "commenter"}

    # Already claimed: a later sign-in finds nothing.
    assert await sharing.claim_email_invites(newcomer.id, newcomer.email) == 0


async def test_claim_skips_a_document_the_account_was_added_to_directly(
    sharing, session, doc, lawyer, invite_emails
):
    """Invited by email, signed up, then added again by account before the
    first verified sign-in: the claim must not collide with that party row."""
    await sharing.add_party(as_auth(lawyer), doc.document_id, NEWCOMER)
    newcomer = await _sign_up(session, NEWCOMER.lower())
    await sharing.add_party(as_auth(lawyer), doc.document_id, newcomer.email)

    assert await sharing.claim_email_invites(newcomer.id, newcomer.email) == 0

    listed = await sharing.get_parties(as_auth(lawyer), doc.document_id)
    assert [(p.user_id, p.status) for p in listed.parties] == [
        (newcomer.id, PARTY_STATUS_PENDING)
    ]


async def test_owner_can_withdraw_an_email_invitation(
    sharing, session, doc, lawyer, invite_emails
):
    party = await sharing.add_party(as_auth(lawyer), doc.document_id, NEWCOMER)

    await sharing.remove_party(as_auth(lawyer), doc.document_id, party.id)

    newcomer = await _sign_up(session, NEWCOMER)
    assert await sharing.claim_email_invites(newcomer.id, newcomer.email) == 0
    with pytest.raises(NotFoundError):
        await sharing.remove_party(as_auth(lawyer), doc.document_id, party.id)


async def test_sign_in_claims_without_failing_on_a_bad_claim(
    sharing, session, doc, lawyer, invite_emails, monkeypatch
):
    from app.features.auth.service import AuthService
    from app.features.documents.workflow.sharing import DocumentSharing

    await sharing.add_party(as_auth(lawyer), doc.document_id, NEWCOMER)
    newcomer = await _sign_up(session, NEWCOMER)
    auth = AuthService(session)

    async def broken(self, *args):
        raise RuntimeError("boom")

    with monkeypatch.context() as m:
        m.setattr(DocumentSharing, "claim_email_invites", broken)
        await auth._claim_document_invites(newcomer.id, newcomer.email)

    await auth._claim_document_invites(newcomer.id, newcomer.email)
    listed = await sharing.get_parties(as_auth(lawyer), doc.document_id)
    assert [p.user_id for p in listed.parties] == [newcomer.id]


async def _expire(session, party) -> None:
    party.invite_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await session.flush()


async def test_an_expired_invitation_is_shown_as_such_and_not_claimed(
    sharing, session, doc, lawyer, invite_emails
):
    party = await sharing.add_party(as_auth(lawyer), doc.document_id, NEWCOMER)
    await _expire(session, party)

    listed = await sharing.get_parties(as_auth(lawyer), doc.document_id)
    assert [p.status for p in listed.parties] == [PARTY_STATUS_EXPIRED]

    newcomer = await _sign_up(session, NEWCOMER)
    assert await sharing.claim_email_invites(newcomer.id, newcomer.email) == 0


async def test_resend_emails_again_and_restarts_the_expiry(
    sharing, session, doc, lawyer, invite_emails
):
    party = await sharing.add_party(as_auth(lawyer), doc.document_id, NEWCOMER)
    await _expire(session, party)

    resent = await sharing.resend_invite(as_auth(lawyer), doc.document_id, party.id)

    assert resent.invite_expires_at > datetime.now(timezone.utc) + timedelta(days=6)
    assert len(invite_emails) == 2
    newcomer = await _sign_up(session, NEWCOMER)
    assert await sharing.claim_email_invites(newcomer.id, newcomer.email) == 1


async def test_resend_is_only_for_email_invitations(
    sharing, session, doc, lawyer, client, other_lawyer, invite_emails
):
    party = await sharing.add_party(as_auth(lawyer), doc.document_id, client.email)
    with pytest.raises(NotFoundError):
        await sharing.resend_invite(as_auth(lawyer), doc.document_id, party.id)

    invite = await sharing.add_party(as_auth(lawyer), doc.document_id, NEWCOMER)
    with pytest.raises(NotFoundError):
        await sharing.resend_invite(as_auth(other_lawyer), doc.document_id, invite.id)


async def test_inviting_an_expired_address_again_reopens_it(
    sharing, session, doc, lawyer, invite_emails
):
    party = await sharing.add_party(as_auth(lawyer), doc.document_id, NEWCOMER)
    await _expire(session, party)

    again = await sharing.add_party(as_auth(lawyer), doc.document_id, NEWCOMER)

    assert again.id == party.id
    assert again.invite_expires_at > datetime.now(timezone.utc)
    assert len(invite_emails) == 2
