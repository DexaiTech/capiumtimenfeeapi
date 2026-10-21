"""Profile pictures: upload, replace, remove, download and who may see them."""

import base64

from httpx import AsyncClient

from tests.conftest import STORAGE_DIR

ME = "/api/v1/users/me"
AVATAR = f"{ME}/avatar"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def stored_files() -> list:
    return sorted(p for p in STORAGE_DIR.rglob("*") if p.is_file() and ".tmp" not in p.parts)


def upload(data: bytes, name: str = "me.png", content_type: str = "image/png"):
    return {"files": {"file": (name, data, content_type)}}


async def test_a_new_user_has_no_picture(client, verified_user):
    assert (await client.get(ME, headers=verified_user["headers"])).json()["data"][
        "avatar_updated_at"
    ] is None
    own = f"/api/v1/users/{verified_user['user']['id']}/avatar"
    assert (await client.get(own, headers=verified_user["headers"])).status_code == 404


async def test_upload_then_download_your_own_picture(client, verified_user):
    headers = verified_user["headers"]
    response = await client.post(AVATAR, headers=headers, **upload(PNG))
    assert response.status_code == 200
    assert response.json()["data"]["avatar_updated_at"] is not None
    assert len(stored_files()) == 1

    shown = await client.get(f"/api/v1/users/{verified_user['user']['id']}/avatar", headers=headers)
    assert shown.status_code == 200
    assert shown.content == PNG
    assert shown.headers["content-type"] == "image/png"
    assert shown.headers["x-content-type-options"] == "nosniff"


async def test_a_new_picture_replaces_the_old_one_and_its_file(client, verified_user):
    headers = verified_user["headers"]
    await client.post(AVATAR, headers=headers, **upload(PNG))
    first = stored_files()
    await client.post(AVATAR, headers=headers, **upload(JPEG, "me.jpg", "image/jpeg"))
    after = stored_files()
    assert len(after) == 1 and after != first
    shown = await client.get(f"/api/v1/users/{verified_user['user']['id']}/avatar", headers=headers)
    assert shown.headers["content-type"] == "image/jpeg"


async def test_removing_the_picture_removes_the_file(client, verified_user):
    headers = verified_user["headers"]
    await client.post(AVATAR, headers=headers, **upload(PNG))
    response = await client.delete(AVATAR, headers=headers)
    assert response.status_code == 200 and response.json()["data"]["avatar_updated_at"] is None
    assert stored_files() == []
    # Removing again is not an error.
    assert (await client.delete(AVATAR, headers=headers)).status_code == 200


async def test_only_pictures_are_accepted(client, verified_user):
    headers = verified_user["headers"]
    not_an_image = await client.post(
        AVATAR, headers=headers, **upload(b"%PDF-1.4 hello", "cv.pdf", "application/pdf")
    )
    assert not_an_image.status_code == 422
    assert not_an_image.json()["error"]["code"] == "FILE_TYPE_NOT_ALLOWED"
    # A name that says PNG over content that is not one.
    liar = await client.post(AVATAR, headers=headers, **upload(b"just text, not a picture"))
    assert liar.status_code == 422
    empty = await client.post(AVATAR, headers=headers, **upload(b""))
    assert empty.status_code == 422 and empty.json()["error"]["code"] == "FILE_EMPTY"
    assert stored_files() == []


async def test_a_picture_over_2_mb_is_refused(client, verified_user):
    big = PNG + b"\x00" * (2 * 1024 * 1024)
    response = await client.post(AVATAR, headers=verified_user["headers"], **upload(big))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert stored_files() == []


async def test_a_picture_needs_a_token(client: AsyncClient):
    assert (await client.post(AVATAR, **upload(PNG))).status_code == 401
    assert (
        await client.get("/api/v1/users/00000000-0000-0000-0000-000000000001/avatar")
    ).status_code == 401


async def test_somebody_you_share_no_practice_with_is_not_found(
    client, verified_user, worker, mailbox
):
    from tests.conftest import PASSWORD, register, run_jobs

    await client.post(AVATAR, headers=verified_user["headers"], **upload(PNG))
    await register(client, "bob@example.com")
    await run_jobs(worker)
    token = mailbox.token_for("bob@example.com")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    login = await client.post(
        "/api/v1/auth/login", json={"email": "bob@example.com", "password": PASSWORD}
    )
    bob = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
    response = await client.get(f"/api/v1/users/{verified_user['user']['id']}/avatar", headers=bob)
    assert response.status_code == 404
