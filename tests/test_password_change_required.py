"""must_change_password: the gate in the authentication dependency, and how it clears."""

import pytest

from tests.conftest import run_jobs
from tests.factories import API, create_user_with_org, error
from tests.foundation_helpers import create_person, sign_in

NEW_PASSWORD = "a brand new secret"


@pytest.fixture
async def newcomer(client):
    """An owner, an organization and a person who still has a temporary password."""
    owner, org = await create_user_with_org(client)
    created = await create_person(client, owner, org, email="temp@example.com")
    person = await sign_in(client, "temp@example.com", created["temporary_password"])
    return owner, org, person, created["temporary_password"]


def change_body(current: str, new: str = NEW_PASSWORD) -> dict:
    return {"current_password": current, "password": new, "password_confirm": new}


async def test_every_route_but_the_allow_list_answers_password_change_required(client, newcomer):
    _, org, person, _ = newcomer
    base = f"{API}/organizations/{org['id']}"
    blocked = [
        ("get", f"{API}/users/me/organizations", None),
        ("get", f"{API}/permissions", None),
        ("get", base, None),
        ("patch", base, {"payment_terms_days": 14}),
        ("get", f"{base}/members", None),
        ("get", f"{base}/members/{person.id}", None),
        ("patch", f"{base}/members/{person.id}", {"initials": "TP"}),
        ("get", f"{base}/roles", None),
        ("get", f"{base}/audit-logs", None),
        ("post", f"{base}/people", {"full_name": "X", "email": "x@example.com"}),
        ("get", f"{API}/users/me/sessions", None),
        ("patch", f"{API}/users/me", {"full_name": "X"}),
        ("post", f"{API}/organizations", {"name": "O"}),
    ]
    for method, url, body in blocked:
        response = await getattr(client, method)(
            url, headers=person.headers, **({"json": body} if body is not None else {})
        )
        assert response.status_code == 403, (method, url)
        assert error(response)["code"] == "PASSWORD_CHANGE_REQUIRED", (method, url)
    # Every route of the business modules goes through the same dependency.
    from app.dependencies.auth import allowed_while_password_change_required

    for method, path in (
        ("GET", f"{base}/clients"),
        ("POST", f"{base}/time-entries"),
        ("GET", f"{base}/invoices"),
        ("PATCH", f"{API}/users/me"),
        ("DELETE", f"{API}/users/me/sessions/x"),
    ):
        assert not allowed_while_password_change_required(method, path), (method, path)


async def test_the_allow_list_still_works(client, newcomer):
    _, _, person, temporary = newcomer
    me = await client.get(f"{API}/users/me", headers=person.headers)
    assert me.status_code == 200 and me.json()["data"]["must_change_password"] is True
    logout_all = await client.post(f"{API}/auth/logout-all", headers=person.headers)
    assert logout_all.status_code == 200
    # Signing in again is part of /auth and works with the temporary password.
    again = await client.post(
        f"{API}/auth/login", json={"email": "temp@example.com", "password": temporary}
    )
    assert again.status_code == 200


async def test_changing_the_password_clears_the_flag_and_keeps_the_session(client, newcomer):
    _, org, person, temporary = newcomer
    changed = await client.post(
        f"{API}/users/me/password", headers=person.headers, json=change_body(temporary)
    )
    assert changed.status_code == 200
    me = await client.get(f"{API}/users/me", headers=person.headers)
    assert me.json()["data"]["must_change_password"] is False
    # The very same access token now opens organization routes.
    ok = await client.get(f"{API}/organizations/{org['id']}", headers=person.headers)
    assert ok.status_code == 200
    login = await client.post(
        f"{API}/auth/login", json={"email": "temp@example.com", "password": NEW_PASSWORD}
    )
    assert (
        login.status_code == 200 and login.json()["data"]["user"]["must_change_password"] is False
    )


async def test_the_new_password_must_differ_from_the_temporary_one(client, newcomer):
    _, _, person, temporary = newcomer
    same = await client.post(
        f"{API}/users/me/password", headers=person.headers, json=change_body(temporary, temporary)
    )
    assert same.status_code == 422
    assert error(same)["details"][0]["field"] == "password"
    me = await client.get(f"{API}/users/me", headers=person.headers)
    assert me.json()["data"]["must_change_password"] is True


async def test_a_wrong_current_password_keeps_the_flag(client, newcomer):
    _, _, person, _ = newcomer
    response = await client.post(
        f"{API}/users/me/password", headers=person.headers, json=change_body("wrong password!")
    )
    assert response.status_code == 400
    me = await client.get(f"{API}/users/me", headers=person.headers)
    assert me.json()["data"]["must_change_password"] is True


async def test_resetting_the_password_by_email_also_clears_the_flag(
    client, newcomer, worker, mailbox
):
    _, _, _, _ = newcomer
    # The "account ready" email (its own reset link, voided by the request below) goes out
    # first; otherwise it may arrive after the new email and be picked up as the newest.
    await run_jobs(worker)
    mailbox.clear()
    assert (
        await client.post(f"{API}/auth/forgot-password", json={"email": "temp@example.com"})
    ).status_code == 202
    await run_jobs(worker)
    token = mailbox.token_for("temp@example.com")
    reset = await client.post(
        f"{API}/auth/reset-password",
        json={"token": token, "password": NEW_PASSWORD, "password_confirm": NEW_PASSWORD},
    )
    assert reset.status_code == 200, reset.text
    login = await client.post(
        f"{API}/auth/login", json={"email": "temp@example.com", "password": NEW_PASSWORD}
    )
    assert login.json()["data"]["user"]["must_change_password"] is False


async def test_people_without_the_flag_are_not_gated(client):
    owner, _org = await create_user_with_org(client)
    assert (
        await client.get(f"{API}/users/me/organizations", headers=owner.headers)
    ).status_code == 200
    assert (await client.get(f"{API}/users/me", headers=owner.headers)).json()["data"][
        "must_change_password"
    ] is False
