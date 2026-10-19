from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import PASSWORD, Mailbox, login, register, run_jobs

URL = "/api/v1/auth/register"


async def test_register_creates_unverified_user_and_sends_link(client, worker, mailbox, db):
    await register(client)
    row = (await db.execute(text("SELECT email_verified_at, password_hash FROM users"))).one()
    assert row.email_verified_at is None
    assert row.password_hash.startswith("$argon2id$")
    assert PASSWORD not in row.password_hash

    assert await run_jobs(worker) == 1
    [message] = mailbox.to("ada@example.com")
    assert message["Subject"] == "Verify your email address"
    assert "http://localhost:5173/verify-email?token=" in Mailbox.text(message)


async def test_verification_token_is_stored_only_as_a_hash(client, worker, mailbox, db):
    await register(client)
    await run_jobs(worker)
    raw = mailbox.token_for("ada@example.com")
    stored = await db.scalar(text("SELECT token_hash FROM email_verification_tokens"))
    assert stored != raw and len(stored) == 64
    # The job payload no longer holds the token once the email is sent.
    payload = await db.scalar(text("SELECT payload FROM background_jobs WHERE type = 'send_email'"))
    assert "token" not in payload


async def test_duplicate_email_gets_the_same_response_and_no_second_account(
    client, worker, mailbox, db
):
    await register(client)
    await run_jobs(worker)
    mailbox.clear()

    response = await client.post(
        URL,
        json={
            "email": "ADA@example.com",
            "password": PASSWORD,
            "password_confirm": PASSWORD,
            "full_name": "Someone Else",
        },
    )
    assert response.status_code == 202
    assert response.json()["message"] == "Check your inbox to verify your email"
    assert await db.scalar(text("SELECT count(*) FROM users")) == 1

    await run_jobs(worker)
    [message] = mailbox.to("ada@example.com")
    assert message["Subject"] == "You already have an account"


async def test_validation_errors_name_the_field(client: AsyncClient):
    response = await client.post(
        URL,
        json={
            "email": "not-an-email",
            "password": "zzqq",
            "password_confirm": "zzqq",
            "full_name": "",
        },
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    fields = {d["field"] for d in error["details"]}
    assert {"email", "password", "full_name"} <= fields
    # The submitted values are not echoed back.
    assert "zzqq" not in response.text


async def test_password_mismatch_and_common_password(client: AsyncClient):
    body = {"email": "a@example.com", "full_name": "A", "password": PASSWORD}
    mismatch = await client.post(URL, json={**body, "password_confirm": PASSWORD + "x"})
    assert mismatch.json()["error"]["details"][0]["field"] == "password_confirm"

    common = await client.post(
        URL, json={**body, "password": "password123", "password_confirm": "password123"}
    )
    assert common.status_code == 422
    assert common.json()["error"]["details"][0]["field"] == "password"


async def test_unknown_fields_are_rejected(client: AsyncClient):
    response = await client.post(
        URL,
        json={
            "email": "a@example.com",
            "full_name": "A",
            "password": PASSWORD,
            "password_confirm": PASSWORD,
            "is_superuser": True,
        },
    )
    assert response.status_code == 422


async def test_verify_email_activates_account_once(client, worker, mailbox, db):
    await register(client)
    await run_jobs(worker)
    token = mailbox.token_for("ada@example.com")

    first = await client.post("/api/v1/auth/verify-email", json={"token": token})
    assert first.status_code == 200
    assert await db.scalar(text("SELECT email_verified_at IS NOT NULL FROM users"))

    again = await client.post("/api/v1/auth/verify-email", json={"token": token})
    assert again.status_code == 422
    assert again.json()["error"]["code"] == "TOKEN_INVALID"

    await run_jobs(worker)
    assert mailbox.to("ada@example.com")[-1]["Subject"].startswith("Welcome")


async def test_expired_verification_token_is_rejected(client, worker, mailbox, db):
    await register(client)
    await run_jobs(worker)
    token = mailbox.token_for("ada@example.com")
    await db.execute(
        text("UPDATE email_verification_tokens SET expires_at = now() - interval '1 minute'")
    )
    await db.commit()
    response = await client.post("/api/v1/auth/verify-email", json={"token": token})
    assert response.json()["error"]["code"] == "TOKEN_INVALID"


async def test_resend_invalidates_the_previous_link(client, worker, mailbox):
    await register(client)
    await run_jobs(worker)
    old = mailbox.token_for("ada@example.com")

    response = await client.post(
        "/api/v1/auth/resend-verification", json={"email": "ada@example.com"}
    )
    assert response.status_code == 202
    await run_jobs(worker)
    new = mailbox.token_for("ada@example.com")
    assert new != old

    assert (await client.post("/api/v1/auth/verify-email", json={"token": old})).status_code == 422
    assert (await client.post("/api/v1/auth/verify-email", json={"token": new})).status_code == 200


async def test_resend_for_unknown_email_is_silent(client, worker, mailbox):
    response = await client.post(
        "/api/v1/auth/resend-verification", json={"email": "nobody@example.com"}
    )
    assert response.status_code == 202
    assert await run_jobs(worker) == 0
    assert mailbox.messages == []


async def test_unverified_user_can_log_in_but_is_held_at_verification(client, worker):
    await register(client)
    data = (await login(client)).json()["data"]
    assert data["user"]["email_verified"] is False
    headers = {"Authorization": f"Bearer {data['access_token']}"}

    assert (await client.get("/api/v1/users/me", headers=headers)).status_code == 200
    blocked = await client.patch("/api/v1/users/me", headers=headers, json={"full_name": "X"})
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "AUTH_EMAIL_NOT_VERIFIED"
