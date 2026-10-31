"""Route-level checks where the router itself builds the response."""

import httpx

from app.features.documents.dependencies import get_drafting
from app.infrastructure.database import get_db
from app.infrastructure.security.dependencies import get_current_user
from app.main import app
from tests.conftest import as_auth, connect_google


async def test_inviting_a_party_answers_pending(session, intake, lawyer, client):
    doc = await intake.open_document(as_auth(lawyer), "Deed")

    async def same_session():
        yield session

    app.dependency_overrides[get_db] = same_session
    app.dependency_overrides[get_current_user] = lambda: as_auth(lawyer)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as http:
            r = await http.post(
                f"/documents/{doc.document_id}/parties",
                json={"email": client.email, "role": "viewer"},
            )
    finally:
        app.dependency_overrides.clear()

    assert r.status_code == 201
    assert (r.json()["status"], r.json()["responded_at"]) == ("pending", None)


async def test_front_end_origin_is_allowed_by_cors():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
        r = await http.options(
            "/documents/",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"


async def post_draft(session, drafting, lawyer, document_id, **form):
    async def same_session():
        yield session

    app.dependency_overrides[get_db] = same_session
    app.dependency_overrides[get_current_user] = lambda: as_auth(lawyer)
    app.dependency_overrides[get_drafting] = lambda: drafting
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as http:
            return await http.post(f"/documents/{document_id}/draft", **form)
    finally:
        app.dependency_overrides.clear()


async def test_draft_is_created_from_form_fields(session, intake, drafting, google, lawyer):
    await connect_google(google, lawyer)
    doc = await intake.open_document(as_auth(lawyer), "Deed")

    r = await post_draft(session, drafting, lawyer, doc.document_id, data={"source": "blank"})

    assert r.status_code == 201
    assert r.json()["draft_url"]


async def test_draft_is_created_from_an_uploaded_word_file(
    session, intake, drafting, drive, google, lawyer
):
    await connect_google(google, lawyer)
    doc = await intake.open_document(as_auth(lawyer), "Deed")
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    r = await post_draft(
        session, drafting, lawyer, doc.document_id,
        data={"source": "upload"},
        files={"file": ("client copy.docx", b"PK\x03\x04 word", docx)},
    )

    assert r.status_code == 201
    file_id = r.json()["draft_url"].split("/d/")[1].split("/")[0]
    assert drive.files[file_id]["content"] == b"PK\x03\x04 word"
