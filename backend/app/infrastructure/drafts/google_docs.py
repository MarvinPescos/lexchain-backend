import httpx

from app.shared.errors.exceptions import BadGatewayError, BadRequestError

from ..google.base import GoogleAuthError
from .base import DraftRef, RemoteAuthor, RemoteComment, RemotePermission, RemoteReply

FILES_URL = "https://www.googleapis.com/drive/v3/files"
# 403s that mean "this token cannot do that" rather than "try again later".
_AUTH_REASONS = {"ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientPermissions"}
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
_FIELDS = "id,name,mimeType,webViewLink"
_AUTHOR = "author(displayName,me,permissionId,photoLink)"
_COMMENT_FIELDS = (
    f"nextPageToken,comments(id,{_AUTHOR},content,quotedFileContent,resolved,"
    f"deleted,createdTime,modifiedTime,"
    f"replies(id,{_AUTHOR},content,action,deleted,createdTime))"
)


class GoogleDocsWorkspace:
    """Working drafts as Google Docs, through the Drive v3 REST API."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(timeout=20)

    async def create(self, access_token: str, name: str) -> DraftRef:
        body = await self._call(
            access_token,
            "POST",
            FILES_URL,
            params={"fields": _FIELDS},
            json={"name": name, "mimeType": GOOGLE_DOC_MIME},
        )
        return self._ref(body)

    async def copy(self, access_token: str, source_file_id: str, name: str) -> DraftRef:
        body = await self._call(
            access_token,
            "POST",
            f"{FILES_URL}/{source_file_id}/copy",
            params={"fields": _FIELDS},
            json={"name": name},
            not_found="LexChain cannot open that template. Pick it again from Drive.",
        )
        return self._ref(body)

    async def get(self, access_token: str, file_id: str) -> DraftRef:
        body = await self._call(
            access_token,
            "GET",
            f"{FILES_URL}/{file_id}",
            params={"fields": _FIELDS},
            not_found="LexChain cannot open that file. Pick it again from Drive.",
        )
        if body.get("mimeType") != GOOGLE_DOC_MIME:
            raise BadRequestError("The working draft must be a Google Doc")
        return self._ref(body)

    async def share(self, access_token: str, file_id: str, email: str, role: str) -> str:
        body = await self._call(
            access_token,
            "POST",
            f"{FILES_URL}/{file_id}/permissions",
            # Google emails the client a "shared with you" link.
            params={"sendNotificationEmail": "true", "fields": "id"},
            json={"type": "user", "role": role, "emailAddress": email},
        )
        return body["id"]

    async def set_role(
        self, access_token: str, file_id: str, permission_id: str, role: str
    ) -> None:
        await self._call(
            access_token,
            "PATCH",
            f"{FILES_URL}/{file_id}/permissions/{permission_id}",
            json={"role": role},
        )

    async def unshare(self, access_token: str, file_id: str, permission_id: str) -> None:
        response = await self.client.delete(
            f"{FILES_URL}/{file_id}/permissions/{permission_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        # Already gone is the state we want.
        if response.status_code == 404:
            return
        self._raise_for(response, None)

    async def list_permissions(
        self, access_token: str, file_id: str
    ) -> list[RemotePermission]:
        body = await self._call(
            access_token,
            "GET",
            f"{FILES_URL}/{file_id}/permissions",
            params={"fields": "permissions(id,role,emailAddress)"},
        )
        return [
            RemotePermission(id=p["id"], role=p["role"], email=p.get("emailAddress"))
            for p in body.get("permissions", [])
        ]

    async def list_comments(self, access_token: str, file_id: str) -> list[RemoteComment]:
        comments: list[RemoteComment] = []
        page_token: str | None = None
        while True:
            params = {
                "fields": _COMMENT_FIELDS,
                "includeDeleted": "true",
                "pageSize": "100",
            }
            if page_token:
                params["pageToken"] = page_token
            body = await self._call(
                access_token, "GET", f"{FILES_URL}/{file_id}/comments", params=params
            )
            comments += [self._comment(c) for c in body.get("comments", [])]
            page_token = body.get("nextPageToken")
            if not page_token:
                return comments

    # ========== helpers =============

    async def _call(
        self,
        access_token: str,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        not_found: str | None = None,
    ) -> dict:
        response = await self.client.request(
            method,
            url,
            params=params,
            json=json,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self._raise_for(response, not_found)
        return response.json() if response.content else {}

    def _raise_for(self, response: httpx.Response, not_found: str | None) -> None:
        if not response.is_error:
            return
        if response.status_code == 401:
            raise GoogleAuthError("Google rejected the access token")
        if response.status_code == 403 and _reason(response) in _AUTH_REASONS:
            raise GoogleAuthError(_reason(response))
        if response.status_code == 404 and not_found:
            raise BadRequestError(not_found)
        raise BadGatewayError(
            f"Google Drive answered {response.status_code}. Try again in a moment."
        )

    def _ref(self, body: dict) -> DraftRef:
        return DraftRef(file_id=body["id"], name=body["name"], url=body["webViewLink"])

    def _author(self, author: dict | None) -> RemoteAuthor:
        author = author or {}
        return RemoteAuthor(
            name=author.get("displayName", "Unknown"),
            me=author.get("me", False),
            permission_id=author.get("permissionId"),
            photo=author.get("photoLink"),
        )

    def _comment(self, c: dict) -> RemoteComment:
        return RemoteComment(
            id=c["id"],
            author=self._author(c.get("author")),
            content=c.get("content", ""),
            quoted_text=(c.get("quotedFileContent") or {}).get("value"),
            resolved=c.get("resolved", False),
            deleted=c.get("deleted", False),
            created_at=c["createdTime"],
            modified_at=c.get("modifiedTime", c["createdTime"]),
            replies=[
                RemoteReply(
                    id=r["id"],
                    author=self._author(r.get("author")),
                    content=r.get("content", ""),
                    action=r.get("action"),
                    deleted=r.get("deleted", False),
                    created_at=r["createdTime"],
                )
                for r in c.get("replies", [])
            ],
        )


def _reason(response: httpx.Response) -> str:
    """Google's machine-readable error reason, from either error format."""
    try:
        error = response.json().get("error", {})
    except ValueError:
        return ""
    for detail in error.get("details", []):
        if detail.get("reason"):
            return detail["reason"]
    errors = error.get("errors") or [{}]
    return errors[0].get("reason", "")
