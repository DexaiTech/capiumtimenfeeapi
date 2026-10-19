from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import BROWSER_HEADERS, PASSWORD, login, register

REFRESH = "/api/v1/auth/refresh"
COOKIE = "ctf_refresh"


async def test_login_returns_access_token_and_sets_scoped_httponly_cookie(client, verified_user):
    response = await login(client)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["token_type"] == "bearer" and data["expires_in"] == 900
    assert data["user"]["email"] == "ada@example.com"
    # No secret in the body (``must_change_password`` is a flag, not a password).
    assert "password_hash" not in response.text and '"password"' not in response.text

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    assert "HttpOnly" in cookie and "Path=/api/v1/auth" in cookie and "SameSite=lax" in cookie


async def test_wrong_password_and_unknown_email_are_indistinguishable(client, verified_user):
    wrong = await login(client, password="wrong-password-123")
    unknown = await login(client, email="nobody@example.com")
    assert wrong.status_code == unknown.status_code == 401
    assert (
        wrong.json()["error"]["code"]
        == unknown.json()["error"]["code"]
        == "AUTH_INVALID_CREDENTIALS"
    )
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_failed_login_is_audited(client, verified_user, db):
    await login(client, password="wrong-password-123")
    assert (
        await db.scalar(text("SELECT count(*) FROM audit_logs WHERE action = 'auth.login_failed'"))
        == 1
    )


async def test_login_is_rate_limited_per_email(client, verified_user):
    # The fixture's own login was the first of five allowed attempts.
    for _ in range(4):
        assert (await login(client, password="wrong-password-123")).status_code == 401
    limited = await login(client)  # even the right password is refused now
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    assert int(limited.headers["retry-after"]) > 0


async def test_disabled_account_cannot_log_in(client, verified_user, db):
    await db.execute(text("UPDATE users SET is_active = false"))
    await db.commit()
    response = await login(client)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_ACCOUNT_DISABLED"


async def test_refresh_rotates_the_token(client: AsyncClient, verified_user, db):
    first = client.cookies[COOKIE]
    response = await client.post(REFRESH, headers=BROWSER_HEADERS)
    assert response.status_code == 200
    assert response.json()["data"]["access_token"]
    assert client.cookies[COOKIE] != first
    used = await db.scalar(text("SELECT count(*) FROM refresh_tokens WHERE used_at IS NOT NULL"))
    assert used == 1


async def test_refresh_requires_custom_header_and_allowed_origin(client, verified_user):
    no_header = await client.post(REFRESH, headers={"Origin": BROWSER_HEADERS["Origin"]})
    assert no_header.status_code == 403
    bad_origin = await client.post(
        REFRESH, headers={"X-Requested-With": "XMLHttpRequest", "Origin": "https://evil.example"}
    )
    assert bad_origin.status_code == 403
    assert bad_origin.json()["error"]["code"] == "PERMISSION_DENIED"


async def test_refresh_without_cookie_is_401(client: AsyncClient):
    response = await client.post(REFRESH, headers=BROWSER_HEADERS)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REFRESH_INVALID"


async def test_concurrent_refresh_within_grace_window_is_tolerated(client, verified_user):
    stale = client.cookies[COOKIE]
    assert (await client.post(REFRESH, headers=BROWSER_HEADERS)).status_code == 200
    # A second tab presents the token that was just rotated.
    client.cookies.clear()
    client.cookies.set(COOKIE, stale)
    assert (await client.post(REFRESH, headers=BROWSER_HEADERS)).status_code == 200


async def test_reuse_after_grace_window_revokes_the_session(client, verified_user, db):
    stolen = client.cookies[COOKIE]
    rotated = await client.post(REFRESH, headers=BROWSER_HEADERS)
    fresh_access = rotated.json()["data"]["access_token"]
    await db.execute(
        text(
            "UPDATE refresh_tokens SET used_at = now() - interval '5 minutes' WHERE used_at IS NOT NULL"
        )
    )
    await db.commit()

    client.cookies.clear()
    client.cookies.set(COOKIE, stolen)
    replay = await client.post(REFRESH, headers=BROWSER_HEADERS)
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "AUTH_REFRESH_INVALID"

    reason = await db.scalar(text("SELECT revoked_reason FROM user_sessions"))
    assert reason == "reuse_detected"
    assert (
        await db.scalar(
            text("SELECT count(*) FROM audit_logs WHERE action = 'auth.refresh_reuse_detected'")
        )
        == 1
    )
    # The legitimate holder is signed out too: the access token dies with the session.
    me = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {fresh_access}"})
    assert me.status_code == 401


async def test_logout_revokes_session_and_access_token(client, verified_user):
    response = await client.post("/api/v1/auth/logout", headers=BROWSER_HEADERS)
    assert response.status_code == 200
    assert COOKIE not in client.cookies
    me = await client.get("/api/v1/users/me", headers=verified_user["headers"])
    assert me.status_code == 401
    assert me.json()["error"]["code"] == "AUTH_TOKEN_INVALID"


async def test_logout_without_cookie_still_succeeds(client: AsyncClient):
    assert (await client.post("/api/v1/auth/logout", headers=BROWSER_HEADERS)).status_code == 200


async def test_logout_all_revokes_every_session(client, verified_user, db):
    await login(client)  # a second device
    response = await client.post("/api/v1/auth/logout-all", headers=verified_user["headers"])
    assert response.json()["data"]["revoked"] == 2
    assert await db.scalar(text("SELECT count(*) FROM user_sessions WHERE revoked_at IS NULL")) == 0


async def test_password_reset_flow(client, verified_user, worker, mailbox, db):
    from tests.conftest import run_jobs

    mailbox.clear()
    assert (
        await client.post("/api/v1/auth/forgot-password", json={"email": "ada@example.com"})
    ).status_code == 202
    await run_jobs(worker)
    token = mailbox.token_for("ada@example.com")
    assert await db.scalar(text("SELECT token_hash FROM password_reset_tokens")) != token

    new_password = "an entirely new passphrase"
    body = {"token": token, "password": new_password, "password_confirm": new_password}
    assert (await client.post("/api/v1/auth/reset-password", json=body)).status_code == 200

    # Every session is revoked, the old password no longer works, the new one does.
    assert (
        await client.get("/api/v1/users/me", headers=verified_user["headers"])
    ).status_code == 401
    assert (await login(client, password=PASSWORD)).status_code == 401
    assert (await login(client, password=new_password)).status_code == 200
    # Single use.
    replay = await client.post("/api/v1/auth/reset-password", json=body)
    assert replay.json()["error"]["code"] == "TOKEN_INVALID"

    await run_jobs(worker)
    assert mailbox.to("ada@example.com")[-1]["Subject"] == "Your password was changed"


async def test_forgot_password_for_unknown_email_sends_nothing(client, worker, mailbox):
    from tests.conftest import run_jobs

    response = await client.post(
        "/api/v1/auth/forgot-password", json={"email": "nobody@example.com"}
    )
    assert response.status_code == 202
    assert await run_jobs(worker) == 0


async def test_new_reset_request_invalidates_the_previous_link(
    client, verified_user, worker, mailbox
):
    from tests.conftest import run_jobs

    mailbox.clear()
    await client.post("/api/v1/auth/forgot-password", json={"email": "ada@example.com"})
    await run_jobs(worker)
    old = mailbox.token_for("ada@example.com")
    await client.post("/api/v1/auth/forgot-password", json={"email": "ada@example.com"})
    await run_jobs(worker)

    body = {
        "token": old,
        "password": "another new passphrase",
        "password_confirm": "another new passphrase",
    }
    assert (await client.post("/api/v1/auth/reset-password", json=body)).status_code == 422


async def test_register_helper_is_rate_limited_per_ip(client: AsyncClient):
    for index in range(5):
        await register(client, email=f"user{index}@example.com")
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "user9@example.com",
            "password": PASSWORD,
            "password_confirm": PASSWORD,
            "full_name": "U",
        },
    )
    assert response.status_code == 429


async def test_auth_rate_limits_can_be_scaled_for_scratch_stacks(client: AsyncClient, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "auth_rate_limit_factor", 3)
    for index in range(15):  # the plain limit is 5 per hour per address
        await register(client, email=f"scaled{index}@example.com")
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "scaled-last@example.com",
            "password": PASSWORD,
            "password_confirm": PASSWORD,
            "full_name": "U",
        },
    )
    assert response.status_code == 429
