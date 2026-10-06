"""Route-level checks where the router itself builds the response."""

import httpx

from app.infrastructure.database import get_db
from app.infrastructure.security.dependencies import get_current_user
from app.main import app
from tests.conftest import as_auth


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
