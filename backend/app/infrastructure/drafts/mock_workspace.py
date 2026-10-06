import uuid
from datetime import datetime, timezone

from app.shared.errors.exceptions import BadRequestError

from .base import DraftRef, RemoteAuthor, RemoteComment, RemotePermission, RemoteReply


class MockDraftWorkspace:
    """In-memory Drive. Shared across instances — a new one is built per
    request — so a dev run behaves like one Drive."""

    files: dict[str, dict] = {}

    async def create(self, access_token: str, name: str) -> DraftRef:
        return self._new(name, kind="doc")

    async def copy(self, access_token: str, source_file_id: str, name: str) -> DraftRef:
        if source_file_id not in self.files:
            raise BadRequestError("LexChain cannot open that template. Pick it again from Drive.")
        return self._new(name, kind="doc")

    async def get(self, access_token: str, file_id: str) -> DraftRef:
        file = self.files.get(file_id)
        if file is None:
            raise BadRequestError("LexChain cannot open that file. Pick it again from Drive.")
        if file["kind"] != "doc":
            raise BadRequestError("The working draft must be a Google Doc")
        return self._ref(file_id)

    async def share(self, access_token: str, file_id: str, email: str, role: str) -> str:
        permission_id = uuid.uuid4().hex
        self.files[file_id]["permissions"][permission_id] = {"email": email, "role": role}
        return permission_id

    async def set_role(
        self, access_token: str, file_id: str, permission_id: str, role: str
    ) -> None:
        self.files[file_id]["permissions"][permission_id]["role"] = role

    async def unshare(self, access_token: str, file_id: str, permission_id: str) -> None:
        self.files[file_id]["permissions"].pop(permission_id, None)

    async def list_permissions(
        self, access_token: str, file_id: str
    ) -> list[RemotePermission]:
        owner = RemotePermission(id="owner", role="owner")
        return [owner] + [
            RemotePermission(id=pid, role=p["role"], email=p["email"])
            for pid, p in self.files[file_id]["permissions"].items()
        ]

    async def list_comments(self, access_token: str, file_id: str) -> list[RemoteComment]:
        return [c.model_copy(deep=True) for c in self.files[file_id]["comments"]]

    # === test / dev helpers: what people do in the Doc ===

    def comment(self, file_id: str, content: str, by: str | None = None) -> str:
        """`by` is a shared email; None means the owner (the lawyer)."""
        comment = RemoteComment(
            id=uuid.uuid4().hex[:10],
            author=self._author(file_id, by),
            content=content,
            created_at=_now(),
            modified_at=_now(),
        )
        self.files[file_id]["comments"].append(comment)
        return comment.id

    def reply(
        self,
        file_id: str,
        comment_id: str,
        content: str = "",
        by: str | None = None,
        action: str | None = None,
    ) -> None:
        comment = self._find(file_id, comment_id)
        comment.replies.append(
            RemoteReply(
                id=uuid.uuid4().hex[:10],
                author=self._author(file_id, by),
                content=content,
                action=action,
                created_at=_now(),
            )
        )
        if action == "resolve":
            comment.resolved = True
        elif action == "reopen":
            comment.resolved = False
        comment.modified_at = _now()

    def resolve(self, file_id: str, comment_id: str, by: str | None = None) -> None:
        self.reply(file_id, comment_id, by=by, action="resolve")

    def reopen(self, file_id: str, comment_id: str, by: str | None = None) -> None:
        self.reply(file_id, comment_id, by=by, action="reopen")

    def _author(self, file_id: str, email: str | None) -> RemoteAuthor:
        if email is None:
            return RemoteAuthor(name="Owner", me=True)
        # Like real Drive: no email and no permission id for other people.
        return RemoteAuthor(name=email.split("@")[0], photo=f"photo-of-{email}")

    def _find(self, file_id: str, comment_id: str) -> RemoteComment:
        return next(c for c in self.files[file_id]["comments"] if c.id == comment_id)

    # === test / dev helpers ===

    def add_existing(self, name: str, kind: str = "doc") -> str:
        """A file the lawyer already had in Drive (what the picker would return)."""
        return self._new(name, kind=kind).file_id

    def share_outside_lexchain(self, file_id: str, email: str) -> None:
        """The lawyer shared the Doc by hand in Google Docs."""
        self.files[file_id]["permissions"][uuid.uuid4().hex] = {
            "email": email,
            "role": "commenter",
        }

    def roles(self, file_id: str) -> dict[str, str]:
        return {p["email"]: p["role"] for p in self.files[file_id]["permissions"].values()}

    def _new(self, name: str, kind: str) -> DraftRef:
        file_id = f"mock-{uuid.uuid4().hex[:12]}"
        self.files[file_id] = {
            "name": name,
            "kind": kind,
            "permissions": {},
            "comments": [],
        }
        return self._ref(file_id)

    def _ref(self, file_id: str) -> DraftRef:
        return DraftRef(
            file_id=file_id,
            name=self.files[file_id]["name"],
            url=f"https://docs.google.com/document/d/{file_id}/edit",
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)
