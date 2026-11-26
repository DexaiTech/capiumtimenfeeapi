"""Two-factor sign-in (the Capium OneLogin MFA rules): set-up and confirmation, the two-step sign-in,
challenges that expire, count wrong codes and work once, codes that cannot be replayed, turning it
off, an administrator's reset, and the secret kept encrypted."""

import time

import pyotp
import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.core import totp
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from tests.clients_helpers import scalar, sql
from tests.conftest import PASSWORD, login
from tests.domain_factories import practice, staff
from tests.factories import API

STEP = int(time.time()) // totp.STEP_SECONDS


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch):
    """Every code is checked against one fixed 30-second step, so no test depends on the clock."""
    monkeypatch.setattr(totp, "current_step", lambda now: STEP)
    monkeypatch.setattr(get_settings(), "auth_rate_limit_factor", 10)


def code(secret: str, offset: int = 0) -> str:
    """The code of the step ``offset`` steps from the frozen one (-1, 0 and 1 are accepted)."""
    return pyotp.TOTP(secret).at((STEP + offset) * totp.STEP_SECONDS)


def wrong(secret: str) -> str:
    good = {code(secret, offset) for offset in (-1, 0, 1)}
    return next(c for c in (f"{n:06d}" for n in range(1_000_000)) if c not in good)


async def turn_on(client: AsyncClient, headers: dict) -> str:
    """Two-factor on for the caller, confirmed with the code of the step before: the steps of now
    and after are left for the sign-ins of the test."""
    setup = await client.post(f"{API}/users/me/2fa/setup", headers=headers)
    assert setup.status_code == 200, setup.text
    secret = setup.json()["data"]["secret"]
    verified = await client.post(
        f"{API}/users/me/2fa/verify", headers=headers, json={"code": code(secret, -1)}
    )
    assert verified.status_code == 200, verified.text
    return secret


async def challenge(client: AsyncClient, email: str = "ada@example.com") -> str:
    started = await login(client, email=email)
    assert started.status_code == 200, started.text
    data = started.json()["data"]
    assert data["requires_two_factor"] is True and data["expires_in"] == 300
    # No session yet: no refresh cookie.
    assert "set-cookie" not in started.headers
    return data["challenge_token"]


async def validate(client: AsyncClient, token: str, value: str):
    return await client.post(
        f"{API}/auth/2fa/validate", json={"challenge_token": token, "code": value}
    )


async def count(action: str) -> int:
    return await scalar("SELECT count(*) FROM audit_logs WHERE action = :a", a=action)


async def test_set_up_needs_a_confirmed_code_before_it_counts(client, verified_user):
    headers = verified_user["headers"]
    setup = await client.post(f"{API}/users/me/2fa/setup", headers=headers)
    assert setup.status_code == 200 and setup.headers["cache-control"] == "no-store"
    data = setup.json()["data"]
    assert len(data["secret"]) == 32
    assert data["otpauth_uri"].startswith("otpauth://totp/")
    assert "secret=" + data["secret"] in data["otpauth_uri"]
    # Still a one-step sign-in until a code is confirmed.
    assert "access_token" in (await login(client)).json()["data"]

    refused = await client.post(
        f"{API}/users/me/2fa/verify", headers=headers, json={"code": wrong(data["secret"])}
    )
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "MFA_CODE_INVALID"
    verified = await client.post(
        f"{API}/users/me/2fa/verify", headers=headers, json={"code": code(data["secret"])}
    )
    assert verified.status_code == 200 and verified.json()["data"]["two_factor_enabled"] is True
    me = await client.get(f"{API}/users/me", headers=headers)
    assert me.json()["data"]["two_factor_enabled"] is True
    assert await count("auth.mfa_enabled") == 1
    again = await client.post(f"{API}/users/me/2fa/setup", headers=headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "MFA_ALREADY_ENABLED"


async def test_confirming_without_a_set_up_is_refused(client, verified_user):
    refused = await client.post(
        f"{API}/users/me/2fa/verify", headers=verified_user["headers"], json={"code": "123456"}
    )
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "MFA_SETUP_REQUIRED"


async def test_the_secret_is_stored_encrypted(client, verified_user):
    secret = await turn_on(client, verified_user["headers"])
    stored = await scalar("SELECT mfa_secret FROM users WHERE email = 'ada@example.com'")
    assert stored and secret not in stored
    assert totp.decrypt_secret(get_settings(), stored) == secret


async def test_turning_it_on_signs_out_the_other_devices(client, verified_user):
    other = (await login(client)).json()["data"]["access_token"]
    await turn_on(client, verified_user["headers"])
    gone = await client.get(f"{API}/users/me", headers={"Authorization": f"Bearer {other}"})
    assert gone.status_code == 401
    kept = await client.get(f"{API}/users/me", headers=verified_user["headers"])
    assert kept.status_code == 200


async def test_sign_in_takes_a_code_after_the_password(client, verified_user):
    secret = await turn_on(client, verified_user["headers"])
    token = await challenge(client)
    signed_in = await validate(client, token, code(secret))
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["data"]["access_token"]
    assert "Path=/api/v1/auth" in signed_in.headers["set-cookie"]
    assert await count("auth.mfa_challenge_started") == 1
    assert await count("auth.mfa_login") == 1
    # The challenge works once.
    reused = await validate(client, token, code(secret, 1))
    assert reused.json()["error"]["code"] == "AUTH_MFA_CHALLENGE_INVALID"


async def test_a_code_is_never_accepted_twice(client, verified_user):
    secret = await turn_on(client, verified_user["headers"])
    assert (await validate(client, await challenge(client), code(secret))).status_code == 200
    replayed = await validate(client, await challenge(client), code(secret))
    assert replayed.status_code == 401
    assert replayed.json()["error"]["code"] == "AUTH_MFA_CODE_INVALID"
    # Nor an older one after a newer one; the next step still works.
    older = await validate(client, await challenge(client), code(secret, -1))
    assert older.json()["error"]["code"] == "AUTH_MFA_CODE_INVALID"
    assert (await validate(client, await challenge(client), code(secret, 1))).status_code == 200


async def test_password_and_code_in_one_request(client, verified_user):
    secret = await turn_on(client, verified_user["headers"])
    refused = await client.post(
        f"{API}/auth/login",
        json={"email": "ada@example.com", "password": PASSWORD, "totp_code": wrong(secret)},
    )
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "AUTH_MFA_CODE_INVALID"
    signed_in = await client.post(
        f"{API}/auth/login",
        json={"email": "ada@example.com", "password": PASSWORD, "totp_code": code(secret)},
    )
    assert signed_in.status_code == 200 and signed_in.json()["data"]["access_token"]


async def test_a_challenge_takes_five_wrong_codes(client, verified_user, monkeypatch):
    # Here only the challenge's own limit: the account lockout is tested below.
    monkeypatch.setattr(get_settings(), "login_lockout_threshold", 50)
    secret = await turn_on(client, verified_user["headers"])
    token = await challenge(client)
    for left in (4, 3, 2, 1):
        refused = await validate(client, token, wrong(secret))
        assert refused.status_code == 401
        error = refused.json()["error"]
        assert (
            error["code"] == "AUTH_MFA_CODE_INVALID" and error["details"]["attempts_left"] == left
        )
    last = await validate(client, token, wrong(secret))
    assert last.json()["error"]["code"] == "AUTH_MFA_TOO_MANY_ATTEMPTS"
    after = await validate(client, token, code(secret))
    assert after.json()["error"]["code"] == "AUTH_MFA_CHALLENGE_INVALID"
    assert await count("auth.mfa_failed") == 5
    attempts = await scalar("SELECT attempt_count FROM mfa_challenges")
    assert attempts == 5


async def test_wrong_codes_count_toward_the_account_lockout(client, verified_user):
    secret = await turn_on(client, verified_user["headers"])
    for _ in range(4):
        assert (await validate(client, await challenge(client), wrong(secret))).status_code == 401
    locked = await validate(client, await challenge(client), wrong(secret))
    assert locked.status_code == 423
    assert locked.json()["error"]["code"] == "AUTH_ACCOUNT_LOCKED"
    assert (await login(client)).status_code == 423


async def test_an_expired_challenge_is_refused(client, verified_user):
    secret = await turn_on(client, verified_user["headers"])
    token = await challenge(client)
    await sql("UPDATE mfa_challenges SET expires_at = NOW(6) - INTERVAL 1 SECOND")
    expired = await validate(client, token, code(secret))
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "AUTH_MFA_CHALLENGE_INVALID"


async def test_an_unknown_challenge_is_refused(client, verified_user):
    refused = await validate(client, "x" * 43, "123456")
    assert refused.json()["error"]["code"] == "AUTH_MFA_CHALLENGE_INVALID"


async def test_turning_it_off_needs_the_password_and_a_code(client, verified_user):
    headers = verified_user["headers"]
    secret = await turn_on(client, headers)
    url = f"{API}/users/me/2fa/disable"
    bad_password = await client.post(
        url, headers=headers, json={"password": "wrong-password-123", "code": code(secret)}
    )
    assert bad_password.status_code == 400
    bad_code = await client.post(
        url, headers=headers, json={"password": PASSWORD, "code": wrong(secret)}
    )
    assert bad_code.status_code == 422
    off = await client.post(url, headers=headers, json={"password": PASSWORD, "code": code(secret)})
    assert off.status_code == 200 and off.json()["data"]["two_factor_enabled"] is False
    assert "access_token" in (await login(client)).json()["data"]
    assert await count("auth.mfa_disabled") == 1
    again = await client.post(url, headers=headers, json={"password": PASSWORD, "code": "123456"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "MFA_NOT_ENABLED"


async def test_an_administrator_resets_a_lost_phone(client: AsyncClient):
    owner, org = await practice(client)
    person = await staff(org)
    await sql(
        "UPDATE users SET password_hash = :h WHERE id = :u",
        h=await hash_password(PASSWORD),
        u=str(person.id),
    )
    await turn_on(client, person.headers)
    url = f"{API}/organizations/{org['id']}/staff/{person.id}"
    assert (await client.get(url, headers=owner.headers)).json()["data"]["two_factor_enabled"]

    reset = await client.post(f"{url}/reset-two-factor", headers=owner.headers)
    assert reset.status_code == 200, reset.text
    assert reset.json()["data"]["two_factor_enabled"] is False
    # Signed out everywhere; the next sign-in is password only.
    assert (await client.get(f"{API}/users/me", headers=person.headers)).status_code == 401
    fresh = (await login(client, email=person.email)).json()["data"]["access_token"]
    assert await count("staff.two_factor_reset") == 1
    again = await client.post(f"{url}/reset-two-factor", headers=owner.headers)
    assert again.status_code == 409 and again.json()["error"]["code"] == "MFA_NOT_ENABLED"
    staff_refused = await client.post(
        f"{API}/organizations/{org['id']}/staff/{owner.id}/reset-two-factor",
        headers={"Authorization": f"Bearer {fresh}"},
    )
    assert staff_refused.status_code == 403


async def test_production_refuses_to_start_without_the_encryption_key():
    with pytest.raises(ValidationError, match="MFA_ENCRYPTION_KEY"):
        Settings(
            app_env="production",
            db_url="mysql://u:p@db/capium",
            jwt_secret_key="k" * 48,
            frontend_url="https://timenfee.example.com",
            backend_url="https://timenfee.example.com",
            mfa_encryption_key="",
        )
