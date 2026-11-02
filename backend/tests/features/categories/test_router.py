"""Authenticated category management API."""

import httpx
import pytest

from app.infrastructure.database import get_db
from app.infrastructure.security.dependencies import get_current_user
from app.main import app
from tests.conftest import as_auth


@pytest.fixture
def http(session):
    async def same_session():
        yield session

    async def call(user, method, path, *, json=None):
        app.dependency_overrides[get_db] = same_session
        if user is not None:
            app.dependency_overrides[get_current_user] = lambda: as_auth(user)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t"
            ) as client:
                return await client.request(method, path, json=json)
        finally:
            app.dependency_overrides.clear()

    return call


async def test_lawyer_can_create_update_and_deactivate_persisted_categories(
    http, lawyer
):
    empty = await http(lawyer, "GET", "/categories/")
    assert empty.status_code == 200
    assert empty.json() == []

    created = await http(
        lawyer, "POST", "/categories/", json={"name": "  Affidavits  "}
    )
    assert created.status_code == 201
    category = created.json()
    assert category["name"] == "Affidavits"
    assert category["active"] is True

    listed = await http(lawyer, "GET", "/categories/")
    assert listed.json() == [category]

    renamed = await http(
        lawyer,
        "PATCH",
        f"/categories/{category['id']}",
        json={"name": "Sworn Affidavits"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Sworn Affidavits"
    assert renamed.json()["active"] is True

    deactivated = await http(
        lawyer, "POST", f"/categories/{category['id']}/deactivate"
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["active"] is False

    persisted = await http(lawyer, "GET", "/categories/")
    assert persisted.json() == [deactivated.json()]

    # Retrying a deactivation after a lost response is safe.
    retried = await http(lawyer, "POST", f"/categories/{category['id']}/deactivate")
    assert retried.status_code == 200
    assert retried.json() == deactivated.json()


async def test_duplicate_names_remain_distinct_categories(http, lawyer):
    first = await http(lawyer, "POST", "/categories/", json={"name": "Affidavits"})
    second = await http(lawyer, "POST", "/categories/", json={"name": "Affidavits"})

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


async def test_categories_are_isolated_between_lawyer_accounts(
    http, lawyer, other_lawyer
):
    created = await http(lawyer, "POST", "/categories/", json={"name": "Contracts"})
    category_id = created.json()["id"]

    other_list = await http(other_lawyer, "GET", "/categories/")
    other_update = await http(
        other_lawyer,
        "PATCH",
        f"/categories/{category_id}",
        json={"name": "Private"},
    )
    other_deactivate = await http(
        other_lawyer, "POST", f"/categories/{category_id}/deactivate"
    )

    assert other_list.status_code == 200
    assert other_list.json() == []
    assert other_update.status_code == 404
    assert other_deactivate.status_code == 404

    owner_list = await http(lawyer, "GET", "/categories/")
    assert owner_list.json()[0]["name"] == "Contracts"
    assert owner_list.json()[0]["active"] is True


@pytest.mark.parametrize(
    "payload",
    [{"name": "   "}, {"name": "x" * 101}],
)
async def test_category_names_are_validated(http, lawyer, payload):
    response = await http(lawyer, "POST", "/categories/", json=payload)
    assert response.status_code == 422


async def test_only_lawyers_can_manage_categories(http, client):
    listed = await http(client, "GET", "/categories/")
    created = await http(client, "POST", "/categories/", json={"name": "Contracts"})

    assert listed.status_code == 403
    assert created.status_code == 403


async def test_category_routes_require_authentication(http):
    response = await http(None, "GET", "/categories/")
    assert response.status_code == 401
