"""Draft comments: mirrored from Google Docs, the right people notified."""

import pytest
from sqlalchemy import select

from app.features.documents.models import DocumentParty
from app.features.notifications.models import Notification, NotificationType
from app.shared.constants import PARTY_STATUS_ACCEPTED
from app.shared.errors.exceptions import ConflictError
from tests.conftest import as_auth, connect_google


async def inbox(session, user) -> list[tuple[NotificationType, str]]:
    rows = await session.execute(
        select(Notification).where(Notification.user_id == user.id).order_by(Notification.created_at)
    )
    return [(n.type, n.body) for n in rows.scalars()]


@pytest.fixture
async def setup(intake, drafting, google, session, lawyer, client):
    """A document with a shared draft, already synced once."""
    me = as_auth(lawyer)
    await connect_google(google, lawyer)
    doc = await intake.open_document(me, "Deed of Sale – Cruz lot")
    session.add(
        DocumentParty(
            document_id=doc.document_id,
            user_id=client.id,
            email=client.email,
            status=PARTY_STATUS_ACCEPTED,
        )
    )
    await session.flush()
    created = await drafting.create_draft(me, doc.document_id)
    file_id = created.draft_url.split("/d/")[1].split("/")[0]
    discussion = drafting.discussion
    await discussion.sync_now(me, doc.document_id)  # baseline
    return doc, file_id, discussion


async def test_client_comment_notifies_the_lawyer(setup, drive, session, lawyer, client):
    doc, file_id, discussion = setup
    drive.comment(file_id, "Fix the lot number", by=client.email)

    result = await discussion.sync_now(as_auth(lawyer), doc.document_id)

    assert result.unresolved == 1
    assert result.comments[0].author_user_id == client.id
    name = client.email.split("@")[0]  # the mock Drive shows the email's name part
    assert await inbox(session, lawyer) == [
        (NotificationType.DRAFT_COMMENT, f"{name}: Fix the lot number")
    ]
    assert await inbox(session, client) == []


async def test_lawyer_question_notifies_clients(setup, drive, session, lawyer, client):
    doc, file_id, discussion = setup
    drive.comment(file_id, "Is the buyer married?")
    await discussion.sync_now(as_auth(lawyer), doc.document_id)

    assert await inbox(session, client) == [
        (NotificationType.DRAFT_COMMENT, "Owner: Is the buyer married?")
    ]
    assert await inbox(session, lawyer) == []


async def test_reply_and_resolve_go_to_the_other_side(setup, drive, session, lawyer, client):
    doc, file_id, discussion = setup
    question = drive.comment(file_id, "Is the buyer married?")
    await discussion.sync_now(as_auth(lawyer), doc.document_id)

    drive.reply(file_id, question, "Yes, to Ana Cruz", by=client.email)
    drive.resolve(file_id, question)  # the lawyer resolves it
    await discussion.sync_now(as_auth(lawyer), doc.document_id)

    name = client.email.split("@")[0]
    assert await inbox(session, lawyer) == [
        (NotificationType.DRAFT_REPLY, f"{name}: Yes, to Ana Cruz")
    ]
    assert (NotificationType.DRAFT_RESOLVED, "Owner resolved: “Is the buyer married?”") in (
        await inbox(session, client)
    )


async def test_client_resolving_own_comment_tells_the_lawyer(
    setup, drive, session, lawyer, client
):
    doc, file_id, discussion = setup
    c = drive.comment(file_id, "Typo in clause 3", by=client.email)
    await discussion.sync_now(as_auth(lawyer), doc.document_id)
    drive.resolve(file_id, c, by=client.email)
    drive.reopen(file_id, c, by=client.email)

    result = await discussion.sync_now(as_auth(lawyer), doc.document_id)

    types = [t for t, _ in await inbox(session, lawyer)]
    assert types == [
        NotificationType.DRAFT_COMMENT,
        NotificationType.DRAFT_RESOLVED,
        NotificationType.DRAFT_REOPENED,
    ]
    assert result.unresolved == 1


async def test_syncing_twice_notifies_once(setup, drive, session, lawyer, client):
    doc, file_id, discussion = setup
    drive.comment(file_id, "Fix the lot number", by=client.email)
    await discussion.sync_now(as_auth(lawyer), doc.document_id)
    await discussion.sync_now(as_auth(lawyer), doc.document_id)
    assert len(await inbox(session, lawyer)) == 1


async def test_first_sync_imports_without_notifying(
    intake, drafting, drive, google, session, lawyer
):
    me = as_auth(lawyer)
    await connect_google(google, lawyer)
    started = drive.add_existing("Started earlier")
    drive.comment(started, "old note to self")
    doc = await intake.open_document(me, "Affidavit")
    await drafting.create_draft(me, doc.document_id, "existing", started)

    result = await drafting.discussion.sync_now(me, doc.document_id)

    assert [c.content for c in result.comments] == ["old note to self"]
    assert await inbox(session, lawyer) == []


async def test_share_added_by_hand_turns_attribution_off(setup, drive, session, lawyer):
    """Drive won't say who a non-lawyer author is. With someone else shared
    outside LexChain, a comment can't be pinned on the client."""
    doc, file_id, discussion = setup
    drive.share_outside_lexchain(file_id, "stranger@gmail.com")
    drive.comment(file_id, "drive-by comment", by="stranger@gmail.com")

    result = await discussion.sync_now(as_auth(lawyer), doc.document_id)

    assert result.comments[0].author_user_id is None
    assert result.comments[0].author_name == "stranger"
    # Still reaches the lawyer: any comment not by the lawyer does.
    assert [t for t, _ in await inbox(session, lawyer)] == [NotificationType.DRAFT_COMMENT]


async def test_several_clients_unattributed_but_all_told_of_the_answer(
    setup, drive, session, sharing, lawyer, client, other_lawyer
):
    """Two parties: Drive can't tell them apart, so a reply on a client's
    thread goes to every client instead of possibly missing the author."""
    doc, file_id, discussion = setup
    party = DocumentParty(
        document_id=doc.document_id, user_id=other_lawyer.id, email=other_lawyer.email
    )
    session.add(party)
    await session.flush()
    await sharing.respond_to_invitation(as_auth(other_lawyer), party.id, accept=True)

    c = drive.comment(file_id, "Fix the lot number", by=client.email)
    result = await discussion.sync_now(as_auth(lawyer), doc.document_id)
    assert result.comments[0].author_user_id is None

    drive.reply(file_id, c, "Done, it's lot 12")
    await discussion.sync_now(as_auth(lawyer), doc.document_id)

    for person in (client, other_lawyer):
        assert (NotificationType.DRAFT_REPLY, "Owner: Done, it's lot 12") in await inbox(
            session, person
        )


async def test_ready_refused_while_comments_are_open(setup, drafting, drive, lawyer, client):
    doc, file_id, _ = setup
    c = drive.comment(file_id, "Fix the lot number", by=client.email)

    with pytest.raises(ConflictError) as refused:
        await drafting.mark_ready(as_auth(lawyer), doc.document_id)
    assert refused.value.details == {"reason": "UNRESOLVED_COMMENTS", "unresolved": 1}

    drive.resolve(file_id, c)
    ready = await drafting.mark_ready(as_auth(lawyer), doc.document_id)
    assert ready.lifecycle == "READY_FOR_SIGNATURE"


async def test_party_can_read_the_threads(setup, drive, lawyer, client):
    doc, file_id, discussion = setup
    drive.comment(file_id, "Fix the lot number", by=client.email)
    await discussion.sync_now(as_auth(lawyer), doc.document_id)

    seen = await discussion.list_comments(as_auth(client), doc.document_id)
    assert [c.content for c in seen.comments] == ["Fix the lot number"]


async def test_notified_people_are_emailed_after_commit(setup, drive, emails, lawyer, client):
    doc, file_id, discussion = setup
    drive.comment(file_id, "Fix the lot number", by=client.email)

    await discussion.sync_now(as_auth(lawyer), doc.document_id)

    assert len(emails) == 1
    assert emails[0]["user_id"] == str(lawyer.id)
    assert emails[0]["subject"] == "New comment on 'Deed of Sale – Cruz lot'"
    assert emails[0]["draft_url"].startswith("https://docs.google.com/document/d/")


async def test_sync_without_commit_queues_nothing(setup, drive, emails, client):
    doc, file_id, discussion = setup
    drive.comment(file_id, "Fix the lot number", by=client.email)
    from app.features.documents.models import Document

    document = await discussion.session.get(Document, doc.document_id)
    await discussion.sync(document)  # no commit, no send_emails
    assert emails == []


async def test_first_comment_on_a_new_draft_notifies(
    intake, drafting, drive, google, session, lawyer, client
):
    """No baseline sync: a Doc LexChain created has no history to import."""
    me = as_auth(lawyer)
    await connect_google(google, lawyer)
    doc = await intake.open_document(me, "Deed")
    session.add(
        DocumentParty(
            document_id=doc.document_id, user_id=client.id, email=client.email,
            status=PARTY_STATUS_ACCEPTED,
        )
    )
    await session.flush()
    created = await drafting.create_draft(me, doc.document_id)
    file_id = created.draft_url.split("/d/")[1].split("/")[0]

    drive.comment(file_id, "Please fix the lot number", by=client.email)
    await drafting.discussion.sync_now(me, doc.document_id)

    assert [t for t, _ in await inbox(session, lawyer)] == [NotificationType.DRAFT_COMMENT]
