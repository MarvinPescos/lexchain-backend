"""The real Drive adapter, against a fake HTTP transport: right endpoints,
right bodies, errors mapped."""

import json

import httpx
import pytest

from app.infrastructure.drafts.google_docs import GoogleDocsWorkspace
from app.infrastructure.google.base import GoogleAuthError
from app.shared.errors.exceptions import BadGatewayError, BadRequestError


def workspace(handler) -> tuple[GoogleDocsWorkspace, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return GoogleDocsWorkspace(client), seen


DOC = {
    "id": "f1",
    "name": "Deed",
    "mimeType": "application/vnd.google-apps.document",
    "webViewLink": "https://docs.google.com/document/d/f1/edit",
}


async def test_create_posts_a_google_doc():
    ws, seen = workspace(lambda r: httpx.Response(200, json=DOC))
    ref = await ws.create("tok", "Deed")

    assert ref.file_id == "f1" and ref.url.endswith("/f1/edit")
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/drive/v3/files"
    assert request.headers["Authorization"] == "Bearer tok"
    assert json.loads(request.content) == {
        "name": "Deed",
        "mimeType": "application/vnd.google-apps.document",
    }


async def test_upload_converts_the_file_to_a_google_doc():
    ws, seen = workspace(lambda r: httpx.Response(200, json=DOC))
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ref = await ws.upload("tok", "Deed", b"PK\x03\x04 word", docx)

    assert ref.file_id == "f1" and ref.url.endswith("/f1/edit")
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/upload/drive/v3/files"
    assert request.url.params["uploadType"] == "multipart"
    assert request.headers["Authorization"] == "Bearer tok"
    assert request.headers["Content-Type"].startswith("multipart/related; boundary=")

    metadata, media = parts(request)
    assert json.loads(metadata[1]) == {
        "name": "Deed",
        "mimeType": "application/vnd.google-apps.document",
    }
    assert media == (f"Content-Type: {docx}".encode(), b"PK\x03\x04 word")


async def test_upload_failure_is_a_bad_gateway():
    ws, _ = workspace(lambda r: httpx.Response(500))
    with pytest.raises(BadGatewayError):
        await ws.upload("tok", "Deed", b"PK", "application/msword")


def parts(request: httpx.Request) -> list[tuple[bytes, bytes]]:
    """The (headers, body) of each part of a multipart/related body."""
    boundary = request.headers["Content-Type"].split("boundary=")[1].encode()
    chunks = request.content.split(b"--" + boundary)[1:-1]
    return [tuple(c.strip(b"\r\n").split(b"\r\n\r\n", 1)) for c in chunks]


async def test_share_returns_the_permission_id():
    ws, seen = workspace(lambda r: httpx.Response(200, json={"id": "perm1"}))
    assert await ws.share("tok", "f1", "cruz@gmail.com", "commenter") == "perm1"
    assert seen[0].url.path == "/drive/v3/files/f1/permissions"
    assert json.loads(seen[0].content) == {
        "type": "user",
        "role": "commenter",
        "emailAddress": "cruz@gmail.com",
    }


async def test_lock_patches_the_role():
    ws, seen = workspace(lambda r: httpx.Response(200, json={"id": "perm1"}))
    await ws.set_role("tok", "f1", "perm1", "reader")
    assert seen[0].method == "PATCH"
    assert seen[0].url.path == "/drive/v3/files/f1/permissions/perm1"
    assert json.loads(seen[0].content) == {"role": "reader"}


async def test_unshare_tolerates_already_removed():
    ws, _ = workspace(lambda r: httpx.Response(404))
    await ws.unshare("tok", "f1", "gone")


async def test_existing_file_must_be_a_doc():
    sheet = {**DOC, "mimeType": "application/vnd.google-apps.spreadsheet"}
    ws, _ = workspace(lambda r: httpx.Response(200, json=sheet))
    with pytest.raises(BadRequestError, match="must be a Google Doc"):
        await ws.get("tok", "f1")


async def test_errors_are_mapped():
    ws, _ = workspace(lambda r: httpx.Response(401))
    with pytest.raises(GoogleAuthError):
        await ws.create("tok", "Deed")

    ws, _ = workspace(lambda r: httpx.Response(500))
    with pytest.raises(BadGatewayError):
        await ws.create("tok", "Deed")

    ws, _ = workspace(lambda r: httpx.Response(404))
    with pytest.raises(BadRequestError, match="cannot open that file"):
        await ws.get("tok", "missing")


async def test_comments_are_parsed_across_pages():
    pages = {
        None: {
            "comments": [
                {
                    "id": "c1",
                    "author": {"displayName": "Cruz", "me": False, "permissionId": "p9"},
                    "content": "Fix the lot number",
                    "quotedFileContent": {"value": "Lot 12"},
                    "resolved": True,
                    "createdTime": "2026-10-01T10:00:00Z",
                    "modifiedTime": "2026-10-01T11:00:00Z",
                    "replies": [
                        {
                            "id": "r1",
                            "author": {"displayName": "Reyes", "me": True},
                            "action": "resolve",
                            "createdTime": "2026-10-01T11:00:00Z",
                        }
                    ],
                }
            ],
            "nextPageToken": "page2",
        },
        "page2": {
            "comments": [
                {
                    "id": "c2",
                    "author": {"displayName": "Reyes", "me": True},
                    "content": "Is the buyer married?",
                    "createdTime": "2026-10-01T12:00:00Z",
                }
            ]
        },
    }
    ws, seen = workspace(
        lambda r: httpx.Response(200, json=pages[r.url.params.get("pageToken")])
    )

    comments = await ws.list_comments("tok", "f1")

    assert [c.id for c in comments] == ["c1", "c2"]
    first = comments[0]
    assert (first.author.permission_id, first.quoted_text, first.resolved) == ("p9", "Lot 12", True)
    assert first.replies[0].action == "resolve" and first.replies[0].author.me
    assert seen[0].url.params["includeDeleted"] == "true"


async def test_missing_scope_asks_to_reconnect():
    body = {"error": {"code": 403, "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}
    ws, _ = workspace(lambda r: httpx.Response(403, json=body))
    with pytest.raises(GoogleAuthError) as refused:
        await ws.create("tok", "Deed")
    assert refused.value.status_code == 409
    assert refused.value.details == {"reason": "GOOGLE_RECONNECT_REQUIRED"}


async def test_permissions_are_listed():
    body = {"permissions": [
        {"id": "o1", "role": "owner", "emailAddress": "reyes@gmail.com"},
        {"id": "p9", "role": "commenter", "emailAddress": "cruz@gmail.com"},
    ]}
    ws, seen = workspace(lambda r: httpx.Response(200, json=body))
    perms = await ws.list_permissions("tok", "f1")
    assert [(p.id, p.role) for p in perms] == [("o1", "owner"), ("p9", "commenter")]
    assert seen[0].url.path == "/drive/v3/files/f1/permissions"
