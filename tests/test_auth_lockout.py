"""Account lockout (the Capium OneLogin rule): consecutive failed sign-ins lock the account for a
while (423), a success resets the count, time, a password reset or an administrator unlocks it,
and an unknown email never reveals anything."""

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.core.security import hash_password
from tests.clients_helpers import scalar, sql
from tests.conftest import PASSWORD, login, run_jobs
from tests.domain_factories import practice, staff
from tests.factories import API

WRONG = "not-the-password-123"


@pytest.fixture(autouse=True)
def _room_above_the_rate_limit(monkeypatch):
    """The per-email rate limit (5 per 15 minutes) would answer 429 from the 6th attempt: these
    tests look past it at the lockout itself."""
    monkeypatch.setattr(get_settings(), "auth_rate_limit_factor", 10)


async def failures(email: str = "ada@example.com") -> int:
    return await scalar("SELECT failed_login_count FROM users WHERE email = :e", e=email)


async def audit_count(action: str) -> int:
    return await scalar("SELECT count(*) FROM audit_logs WHERE action = :a", a=action)


async def test_five_wrong_passwords_lock_the_account_even_for_the_right_password(
    client: AsyncClient, verified_user
):
    for attempt in range(1, 5):
        refused = await login(client, password=WRONG)
        assert refused.status_code == 401, attempt
        assert refused.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"
        assert await failures() == attempt
    locked = await login(client, password=WRONG)
    assert locked.status_code == 423
    error = locked.json()["error"]
    assert error["code"] == "AUTH_ACCOUNT_LOCKED" and "15 minutes" in error["message"]
    assert 890 <= error["details"]["retry_after"] <= 900
    assert int(locked.headers["retry-after"]) == error["details"]["retry_after"]

    still_locked = await login(client)
    assert still_locked.status_code == 423
    assert still_locked.json()["error"]["code"] == "AUTH_ACCOUNT_LOCKED"
    assert await audit_count("auth.account_locked") == 1
    assert await audit_count("auth.login_locked") == 1
    assert await audit_count("auth.login_failed") == 5


async def test_a_successful_sign_in_starts_the_count_again(client: AsyncClient, verified_user):
    for _ in range(4):
        assert (await login(client, password=WRONG)).status_code == 401
    assert (await login(client)).status_code == 200
    assert await failures() == 0
    for _ in range(4):
        assert (await login(client, password=WRONG)).status_code == 401
    assert (await login(client)).status_code == 200


async def test_the_lock_passes_with_time(client: AsyncClient, verified_user):
    for _ in range(5):
        await login(client, password=WRONG)
    assert (await login(client)).status_code == 423
    await sql(
        "UPDATE users SET locked_until = NOW(6) - INTERVAL 1 SECOND WHERE email = :e",
        e="ada@example.com",
    )
    assert (await login(client)).status_code == 200
    assert await scalar("SELECT locked_until FROM users WHERE email = 'ada@example.com'") is None


async def test_a_password_reset_unlocks_the_account(
    client: AsyncClient, verified_user, worker, mailbox
):
    for _ in range(5):
        await login(client, password=WRONG)
    asked = await client.post(f"{API}/auth/forgot-password", json={"email": "ada@example.com"})
    assert asked.status_code in (200, 202), asked.text
    await run_jobs(worker)
    new_password = "a brand new passphrase"
    reset = await client.post(
        f"{API}/auth/reset-password",
        json={
            "token": mailbox.token_for("ada@example.com"),
            "password": new_password,
            "password_confirm": new_password,
        },
    )
    assert reset.status_code == 200, reset.text
    assert (await login(client, password=new_password)).status_code == 200


async def test_an_unknown_email_is_never_locked_and_answers_like_a_wrong_password(
    client: AsyncClient, verified_user
):
    for _ in range(7):
        refused = await login(client, email="nobody@example.com", password=WRONG)
        assert refused.status_code == 401
        assert refused.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"
    assert await audit_count("auth.account_locked") == 0
    # The real account was never touched.
    assert await failures() == 0
    assert (await login(client)).status_code == 200


async def set_password(user_id) -> None:
    await sql(
        "UPDATE users SET password_hash = :h WHERE id = :u",
        h=await hash_password(PASSWORD),
        u=str(user_id),
    )


async def test_an_administrator_unlocks_a_staff_member(client: AsyncClient):
    owner, org = await practice(client)
    person = await staff(org)
    await set_password(person.id)
    for _ in range(5):
        await login(client, email=person.email, password=WRONG)
    assert (await login(client, email=person.email)).status_code == 423

    url = f"{API}/organizations/{org['id']}/staff/{person.id}"
    detail = await client.get(url, headers=owner.headers)
    assert detail.json()["data"]["locked_until"] is not None

    unlocked = await client.post(f"{url}/unlock-login", headers=owner.headers)
    assert unlocked.status_code == 200, unlocked.text
    assert unlocked.json()["data"]["locked_until"] is None
    assert (await login(client, email=person.email)).status_code == 200
    rows = await sql(
        "SELECT target_id, organization_id FROM audit_logs WHERE action = 'staff.login_unlocked'"
    )
    assert len(rows) == 1 and str(rows[0]["target_id"]) == str(person.id)
    assert str(rows[0]["organization_id"]) == org["id"]

    # Nothing to unlock: answered, nothing recorded.
    again = await client.post(f"{url}/unlock-login", headers=owner.headers)
    assert again.status_code == 200
    assert await audit_count("staff.login_unlocked") == 1


async def test_unlocking_needs_member_remove_and_is_not_for_oneself(client: AsyncClient):
    owner, org = await practice(client)
    person = await staff(org)
    other = await staff(org)
    url = f"{API}/organizations/{org['id']}/staff"
    refused = await client.post(f"{url}/{other.id}/unlock-login", headers=person.headers)
    assert refused.status_code == 403
    own = await client.post(f"{url}/{owner.id}/unlock-login", headers=owner.headers)
    assert own.status_code == 403
