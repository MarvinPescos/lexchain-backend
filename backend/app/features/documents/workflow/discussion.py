"""Comments on the working draft: mirrored from Google Docs, notified in LexChain.

`sync` reads every thread on the draft, stores what is new, and notifies the
other people in each thread — in-app, and by email once the caller has
committed (`send_emails`). It is idempotent — running it twice notifies
nobody twice — and runs on a schedule while the document is PREPARING, on
demand, and right before the document is marked ready for signature.
"""

import uuid
from datetime import datetime, timezone

from app.features.google.service import GoogleConnectionService
from app.features.notifications.models import NotificationType
from app.features.notifications.service import NotificationService
from app.infrastructure.drafts import DraftWorkspace, RemoteAuthor, RemoteComment
from app.infrastructure.observability.logging_setup import log
from app.infrastructure.security.dependencies import AuthenticatedUser
from app.shared.constants import PARTY_STATUS_ACCEPTED
from sqlalchemy.ext.asyncio import AsyncSession

from ..access import DocumentAccess
from ..models import Document, DocumentParty, DraftComment, DraftReply
from ..repository import DocumentRepository
from ..schemas import DraftCommentListResponse, DraftCommentResponse

# What happened in a thread, and how it is announced.
_EVENT_COMMENT = "comment"
_EVENT_REPLY = "reply"
_EVENT_RESOLVE = "resolve"
_EVENT_REOPEN = "reopen"

_NOTIFICATION = {
    _EVENT_COMMENT: (NotificationType.DRAFT_COMMENT, "New comment on '{doc}'"),
    _EVENT_REPLY: (NotificationType.DRAFT_REPLY, "New reply on '{doc}'"),
    _EVENT_RESOLVE: (NotificationType.DRAFT_RESOLVED, "Comment resolved on '{doc}'"),
    _EVENT_REOPEN: (NotificationType.DRAFT_REOPENED, "Comment reopened on '{doc}'"),
}

_PREVIEW = 140


class _Event:
    def __init__(
        self,
        kind: str,
        comment: DraftComment,
        actor_id: uuid.UUID | None,
        actor_name: str,
        text: str,
    ) -> None:
        self.kind = kind
        self.comment = comment
        self.actor_id = actor_id
        self.actor_name = actor_name
        self.text = text


class DocumentDiscussion:
    def __init__(
        self,
        session: AsyncSession,
        workspace: DraftWorkspace,
        google: GoogleConnectionService,
    ) -> None:
        self.session = session
        self.workspace = workspace
        self.google = google
        self.repo = DocumentRepository(session)
        self.access = DocumentAccess(session)
        self.notifications = NotificationService(session)
        # Emails for notifications created by `sync`, sent by `send_emails`
        # once the caller has committed them.
        self._pending_emails: list[dict] = []

    # === API ===

    async def list_comments(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> DraftCommentListResponse:
        """The mirrored threads as of the last sync. Owner and parties."""
        document = await self.access.authorize(user, document_id)
        return await self._response(document)

    async def sync_now(
        self, user: AuthenticatedUser, document_id: uuid.UUID
    ) -> DraftCommentListResponse:
        """Pull the latest comments from Google Docs right away."""
        document = await self.access.authorize(user, document_id)
        await self.sync(document)
        await self.session.commit()
        self.send_emails()
        return await self._response(document)

    def send_emails(self) -> None:
        """Queue the emails for what `sync` notified. Call after committing:
        a rolled-back sync must not email about comments it never stored."""
        from app.celery_tasks import send_draft_activity_email

        pending, self._pending_emails = self._pending_emails, []
        for email in pending:
            try:
                send_draft_activity_email.delay(**email)
            except Exception as e:
                # The in-app notification is already saved; a lost email is
                # not worth failing the request over.
                log.error("document.draft.email_enqueue_failed", error=str(e))

    # === Sync ===

    async def sync(self, document: Document) -> int:
        """Mirror the draft's comments and notify. Returns the open thread count.

        Does not commit — the caller owns the transaction.
        """
        if not document.draft_file_id:
            return 0

        token = await self.google.access_token(document.user_id)
        remote = await self.workspace.list_comments(token, document.draft_file_id)

        parties = await self.repo.get_parties(document.id)
        by_permission = {
            p.draft_permission_id: p for p in parties if p.draft_permission_id
        }
        stored = {c.external_id: c for c in await self.repo.get_draft_comments(document.id)}
        sole_party = await self._sole_party(token, document, by_permission)

        def author_id(author: RemoteAuthor) -> uuid.UUID | None:
            """Who wrote it, as far as Drive lets us know.

            `me` is the lawyer. Drive withholds who anyone else is, so a party
            is only named when the draft is shared with exactly one person
            besides the lawyer and LexChain created that share. Otherwise the
            author stays unattributed (None) and keeps Google's display name.
            """
            if author.me:
                return document.user_id
            party = by_permission.get(author.permission_id)
            if party:
                return party.user_id
            return sole_party.user_id if sole_party else None

        events: list[_Event] = []
        for rc in remote:
            comment = stored.get(rc.id)
            if comment is None:
                comment = self._new_comment(document, rc, author_id(rc.author))
                self.session.add(comment)
                if not rc.deleted:
                    events.append(
                        _Event(
                            _EVENT_COMMENT,
                            comment,
                            comment.author_user_id,
                            rc.author.name,
                            rc.content,
                        )
                    )
            else:
                comment.content = rc.content
                comment.quoted_text = rc.quoted_text
                comment.resolved = rc.resolved
                comment.deleted = rc.deleted
                # Attribution can become possible later (e.g. a second share
                # was removed); fill it in, never overwrite a known author.
                if comment.author_user_id is None:
                    comment.author_user_id = author_id(rc.author)

            known = {r.external_id for r in comment.replies}
            for rr in rc.replies:
                if rr.id in known:
                    continue
                reply = DraftReply(
                    external_id=rr.id,
                    author_user_id=author_id(rr.author),
                    author_name=rr.author.name,
                    content=rr.content,
                    action=rr.action,
                    deleted=rr.deleted,
                    remote_created_at=rr.created_at,
                )
                comment.replies.append(reply)
                if not rr.deleted:
                    kind = {"resolve": _EVENT_RESOLVE, "reopen": _EVENT_REOPEN}.get(
                        rr.action or "", _EVENT_REPLY
                    )
                    text = rr.content if kind == _EVENT_REPLY else comment.content
                    events.append(
                        _Event(kind, comment, reply.author_user_id, rr.author.name, text)
                    )

        # The first sync imports what was already there (e.g. an existing Doc
        # the lawyer picked) without flooding anyone with notifications.
        first_sync = document.comments_synced_at is None
        document.comments_synced_at = datetime.now(timezone.utc)
        await self.session.flush()

        if not first_sync:
            for event in events:
                await self._notify(document, parties, event)

        unresolved = sum(
            1 for c in await self.repo.get_draft_comments(document.id)
            if not c.resolved and not c.deleted
        )
        log.info(
            "document.draft.sync",
            document_id=str(document.id),
            threads=len(remote),
            events=len(events),
            notified=not first_sync,
            unresolved=unresolved,
        )
        return unresolved

    # ========== helpers =============

    async def _sole_party(
        self,
        token: str,
        document: Document,
        by_permission: dict[str, DocumentParty],
    ) -> DocumentParty | None:
        """The one party who can comment, if the draft has exactly one.

        Read from Drive each sync, so a share the lawyer added by hand in
        Google Docs turns attribution by elimination off instead of
        misattributing that person's comments.
        """
        permissions = await self.workspace.list_permissions(
            token, document.draft_file_id
        )
        others = [p for p in permissions if p.role != "owner"]
        if len(others) != 1:
            return None
        party = by_permission.get(others[0].id)
        return party if party and party.status == PARTY_STATUS_ACCEPTED else None

    def _new_comment(
        self, document: Document, rc: RemoteComment, author_user_id: uuid.UUID | None
    ) -> DraftComment:
        return DraftComment(
            document_id=document.id,
            external_id=rc.id,
            author_user_id=author_user_id,
            author_name=rc.author.name,
            content=rc.content,
            quoted_text=rc.quoted_text,
            resolved=rc.resolved,
            deleted=rc.deleted,
            remote_created_at=rc.created_at,
            replies=[],
        )

    def _recipients(
        self, document: Document, parties: list[DocumentParty], event: _Event
    ) -> set[uuid.UUID]:
        """Everyone in the thread plus the lawyer, minus whoever acted.

        A new comment has no thread yet: the lawyer's goes to the clients, a
        client's goes to the lawyer.
        """
        if event.kind == _EVENT_COMMENT:
            if event.actor_id == document.user_id:
                recipients = {
                    p.user_id for p in parties if p.status == PARTY_STATUS_ACCEPTED
                }
            else:
                recipients = {document.user_id}
        else:
            participants = {event.comment.author_user_id}
            participants |= {r.author_user_id for r in event.comment.replies}
            recipients = {document.user_id} | participants
            if None in participants:
                # A client is in this thread but Drive won't say which one:
                # tell every client on the document rather than miss them.
                recipients |= {
                    p.user_id for p in parties if p.status == PARTY_STATUS_ACCEPTED
                }
        recipients.discard(None)
        recipients.discard(event.actor_id)
        return recipients

    async def _notify(
        self, document: Document, parties: list[DocumentParty], event: _Event
    ) -> None:
        notification_type, title = _NOTIFICATION[event.kind]
        text = event.text if len(event.text) <= _PREVIEW else event.text[:_PREVIEW] + "…"
        verb = {_EVENT_RESOLVE: "resolved", _EVENT_REOPEN: "reopened"}.get(event.kind)
        body = f"{event.actor_name} {verb}: “{text}”" if verb else f"{event.actor_name}: {text}"
        subject = title.format(doc=document.file_name)
        for user_id in self._recipients(document, parties, event):
            self._pending_emails.append(
                {
                    "user_id": str(user_id),
                    "subject": subject,
                    "body": body,
                    "document_name": document.file_name,
                    "draft_url": document.draft_url,
                }
            )
            await self.notifications.create(
                user_id=user_id,
                type=notification_type,
                title=subject,
                body=body,
                event_metadata={
                    "document_id": str(document.id),
                    "comment_id": str(event.comment.id),
                    "draft_url": document.draft_url,
                },
            )

    async def _response(self, document: Document) -> DraftCommentListResponse:
        comments = await self.repo.get_draft_comments(document.id)
        visible = [c for c in comments if not c.deleted]
        return DraftCommentListResponse(
            document_id=document.id,
            synced_at=document.comments_synced_at,
            unresolved=sum(1 for c in visible if not c.resolved),
            comments=[DraftCommentResponse.from_comment(c, document) for c in visible],
        )
