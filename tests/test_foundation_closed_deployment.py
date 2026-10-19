"""CLOSED_DEPLOYMENT: one practice, no public sign-up, accounts made by an administrator."""

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from tests.conftest import PASSWORD
from tests.factories import API, create_user, create_user_with_org, error
from tests.foundation_helpers import create_person, sign_in


@pytest.fixture
def closed(monkeypatch):
    monkeypatch.setattr(get_settings(), "closed_deployment", True)


async def test_system_reports_an_open_deployment_without_a_practice(client: AsyncClient):
    await create_user_with_org(client, "Acme")
    response = await client.get(f"{API}/system")
    assert response.status_code == 200
    assert response.json()["data"] == {"closed_deployment": False, "organization": None}


async def test_system_names_the_one_practice_of_a_closed_deployment(
    client: AsyncClient, monkeypatch
):
    _, org = await create_user_with_org(client, "Smith & Co")
    monkeypatch.setattr(get_settings(), "closed_deployment", True)
    response = await client.get(f"{API}/system")
    assert response.json()["data"] == {
        "closed_deployment": True,
        "organization": {"name": "Smith & Co", "slug": org["slug"]},
    }


async def test_system_is_empty_before_the_bootstrap(client: AsyncClient, closed):
    response = await client.get(f"{API}/system")
    assert response.json()["data"] == {"closed_deployment": True, "organization": None}


async def test_registration_is_refused_before_validation(client: AsyncClient, closed):
    for body in (
        {},
        {
            "email": "new@example.com",
            "password": PASSWORD,
            "password_confirm": PASSWORD,
            "full_name": "New",
        },
    ):
        response = await client.post(f"{API}/auth/register", json=body)
        assert response.status_code == 403
        assert error(response)["code"] == "REGISTRATION_DISABLED"


async def test_practice_creation_is_refused(client: AsyncClient, closed):
    user = await create_user()
    for body in ({}, {"name": "Second practice"}):
        response = await client.post(f"{API}/organizations", headers=user.headers, json=body)
        assert response.status_code == 403
        assert error(response)["code"] == "ORGANIZATION_CREATION_DISABLED"
    # Even without a token the answer is the same: there is nothing to sign up for.
    anonymous = await client.post(f"{API}/organizations", json={"name": "X"})
    assert anonymous.status_code == 403


async def test_staff_accounts_are_still_made_by_an_administrator(client: AsyncClient, monkeypatch):
    owner, org = await create_user_with_org(client)
    monkeypatch.setattr(get_settings(), "closed_deployment", True)
    created = await create_person(client, owner, org, email="joiner@example.com")
    person = await sign_in(client, "joiner@example.com", created["temporary_password"])
    me = await client.get(f"{API}/users/me", headers=person.headers)
    assert me.status_code == 200 and me.json()["data"]["must_change_password"] is True
