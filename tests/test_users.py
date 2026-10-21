from httpx import AsyncClient
from sqlalchemy import text

from tests.conftest import PASSWORD, login, register

ME = "/api/v1/users/me"


async def test_me_returns_the_profile_without_secrets(client, verified_user):
    response = await client.get(ME, headers=verified_user["headers"])
    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == {
        "id",
        "email",
        "full_name",
        "timezone",
        "email_verified",
        "must_change_password",
        "created_at",
        "avatar_updated_at",
    }
    assert data["email"] == "ada@example.com" and data["timezone"] == "Europe/London"
    assert data["email_verified"] is True and data["must_change_password"] is False


async def test_me_requires_a_token(client: AsyncClient):
    response = await client.get(ME)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


async def test_garbage_and_tampered_tokens_are_rejected(client, verified_user):
    garbage = await client.get(ME, headers={"Authorization": "Bearer not.a.jwt"})
    assert garbage.json()["error"]["code"] == "AUTH_TOKEN_INVALID"
    tampered = verified_user["access_token"][:-4] + "AAAA"
    response = await client.get(ME, headers={"Authorization": f"Bearer {tampered}"})
    assert response.status_code == 401


async def test_update_profile(client, verified_user):
    response = await client.patch(
        ME,
        headers=verified_user["headers"],
        json={"full_name": "  Ada King  ", "timezone": "Asia/Kolkata"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["full_name"] == "Ada King" and data["timezone"] == "Asia/Kolkata"


async def test_update_profile_rejects_unknown_timezone_and_server_fields(client, verified_user):
    headers = verified_user["headers"]
    assert (
        await client.patch(ME, headers=headers, json={"timezone": "Mars/Olympus"})
    ).status_code == 422
    assert (
        await client.patch(ME, headers=headers, json={"email_verified": True})
    ).status_code == 422
    assert (await client.patch(ME, headers=headers, json={"is_superuser": True})).status_code == 422


async def test_change_password_requires_current_and_revokes_other_sessions(
    client, verified_user, db
):
    await login(client)  # second device
    headers = verified_user["headers"]
    new = "a brand new passphrase"
    wrong = await client.post(
        f"{ME}/password",
        headers=headers,
        json={"current_password": "nope-nope-nope", "password": new, "password_confirm": new},
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"]["code"] == "AUTH_INVALID_CREDENTIALS"

    ok = await client.post(
        f"{ME}/password",
        headers=headers,
        json={"current_password": PASSWORD, "password": new, "password_confirm": new},
    )
    assert ok.status_code == 200
    assert await db.scalar(text("SELECT count(*) FROM user_sessions WHERE revoked_at IS NULL")) == 1
    # This device stays signed in.
    assert (await client.get(ME, headers=headers)).status_code == 200
    assert (await login(client, password=new)).status_code == 200


async def test_sessions_list_marks_current_and_revoke_works(client, verified_user):
    second = (await login(client)).json()["data"]["access_token"]
    headers = verified_user["headers"]
    sessions = (await client.get(f"{ME}/sessions", headers=headers)).json()["data"]
    assert len(sessions) == 2
    assert sum(s["is_current"] for s in sessions) == 1
    other = next(s for s in sessions if not s["is_current"])

    assert (await client.delete(f"{ME}/sessions/{other['id']}", headers=headers)).status_code == 200
    assert (await client.get(ME, headers={"Authorization": f"Bearer {second}"})).status_code == 401
    assert len((await client.get(f"{ME}/sessions", headers=headers)).json()["data"]) == 1


async def test_cannot_revoke_another_users_session(client, verified_user, db):
    await register(client, email="grace@example.com")
    grace = (await login(client, email="grace@example.com")).json()["data"]
    ada_session = await db.scalar(
        text(
            "SELECT s.id FROM user_sessions s JOIN users u ON u.id = s.user_id WHERE u.email = 'ada@example.com'"
        )
    )
    response = await client.delete(
        f"{ME}/sessions/{ada_session}", headers={"Authorization": f"Bearer {grace['access_token']}"}
    )
    # Outside the caller's scope looks exactly like "does not exist".
    assert response.status_code == 404
    assert (await client.get(ME, headers=verified_user["headers"])).status_code == 200


async def test_profile_name_cannot_be_blanked(client, verified_user):
    headers = verified_user["headers"]
    for body in ({"full_name": ""}, {"full_name": "   "}, {"full_name": "x" * 121}):
        response = await client.patch(ME, headers=headers, json=body)
        assert response.status_code == 422, body
    # An explicit null leaves the name as it is.
    kept = await client.patch(ME, headers=headers, json={"full_name": None})
    assert kept.status_code == 200 and kept.json()["data"]["full_name"] == "Ada Lovelace"


async def test_pictures_are_never_served_from_a_public_route(client, verified_user):
    """The picture is set with an authenticated upload and read with the caller's token (see
    test_avatar); the sibling product's public avatar URL does not exist here."""
    headers = verified_user["headers"]
    for method, url in (("get", "/api/v1/public/avatars/00000000-0000-7000-8000-000000000000"),):
        response = await getattr(client, method)(url, headers=headers)
        assert response.status_code in (404, 405), (method, url)
