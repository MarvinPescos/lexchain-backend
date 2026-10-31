from datetime import datetime
from typing import Protocol

from pydantic import BaseModel

# Drive permission roles LexChain uses on a working draft.
ROLE_WRITER = "writer"
ROLE_COMMENTER = "commenter"
ROLE_READER = "reader"


class DraftRef(BaseModel):
    """A working draft in the lawyer's Drive."""

    file_id: str
    name: str
    url: str


class RemoteAuthor(BaseModel):
    """Who wrote a comment or reply, as Drive reports it.

    Only `me` is dependable: every call runs with the lawyer's token, so `me`
    marks the lawyer. For anyone else Drive withholds the email and, in
    practice, the permission id too — `name` and `photo` are profile values
    that do not match the sharing permission.
    """

    name: str
    me: bool = False
    permission_id: str | None = None
    photo: str | None = None


class RemoteReply(BaseModel):
    id: str
    author: RemoteAuthor
    content: str = ""
    # "resolve" / "reopen" when the reply resolved or reopened the thread.
    action: str | None = None
    deleted: bool = False
    created_at: datetime


class RemoteComment(BaseModel):
    id: str
    author: RemoteAuthor
    content: str = ""
    quoted_text: str | None = None
    resolved: bool = False
    deleted: bool = False
    created_at: datetime
    modified_at: datetime
    replies: list[RemoteReply] = []


class RemotePermission(BaseModel):
    """Someone the draft is shared with."""

    id: str
    role: str
    email: str | None = None


class DraftWorkspace(Protocol):
    """Where Path 1 working drafts live. Every call acts as the lawyer, with
    their access token. Never reads the draft's content — the signed PDF is
    the legal document."""

    async def create(self, access_token: str, name: str) -> DraftRef: ...

    async def copy(self, access_token: str, source_file_id: str, name: str) -> DraftRef: ...

    async def upload(
        self, access_token: str, name: str, content: bytes, content_type: str
    ) -> DraftRef:
        """A file from the lawyer's computer, converted to a Google Doc."""
        ...

    async def get(self, access_token: str, file_id: str) -> DraftRef:
        """An existing Doc the lawyer picked. Refuses anything not a Google Doc."""
        ...

    async def share(
        self, access_token: str, file_id: str, email: str, role: str
    ) -> str:
        """Give `email` access with `role`; returns the Drive permission id."""
        ...

    async def set_role(
        self, access_token: str, file_id: str, permission_id: str, role: str
    ) -> None: ...

    async def unshare(self, access_token: str, file_id: str, permission_id: str) -> None: ...

    async def list_permissions(
        self, access_token: str, file_id: str
    ) -> list[RemotePermission]:
        """Everyone with access to the draft, owner included."""
        ...

    async def list_comments(self, access_token: str, file_id: str) -> list[RemoteComment]:
        """Every comment thread on the draft, with replies, deleted ones included."""
        ...
