import pytest
from httpx import AsyncClient

from app.core.permissions import ALL_CODES, SYSTEM_ROLES
from tests.factories import (
    API,
    add_member,
    audit_entries,
    create_org,
    create_user,
    create_user_with_org,
    error,
)

ORGS = f"{API}/organizations"
MINE = f"{API}/users/me/organizations"


async def delete_org(client: AsyncClient, actor, org: dict, confirm: str | None = None):
    return await client.request(
        "DELETE",
        f"{ORGS}/{org['id']}",
        headers=actor.headers,
        json={"confirm_name": org["name"] if confirm is None else confirm},
    )


# --- create ---


async def test_create_makes_the_caller_owner_and_copies_the_system_roles(client):
    ada = await create_user("Ada")
    response = await client.post(ORGS, headers=ada.headers, json={"name": "  Acme Inc  "})
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    org = data["organization"]
    assert org["name"] == "Acme Inc" and org["slug"] == "acme-inc"
    # The timezone defaults to the creator's (Europe/London unless they changed it).
    assert org["timezone"] == "Europe/London" and org["week_start"] == 1
    assert org["working_days"] == [1, 2, 3, 4, 5] and org["minutes_per_day"] == 450
    assert org["currency"] == "GBP" and org["vat_registered"] is True
    assert org["payment_terms_days"] == 30 and org["default_charge_rate"] == 0
    assert org["time_increment_minutes"] == 6 and org["require_timesheet_approval"] is True
    assert org["country"] == "GB" and org["created_at"].endswith("Z")
    assert data["role"]["system_key"] == "owner" and data["role"]["name"] == "Owner"
    assert set(data["permissions"]) == ALL_CODES

    roles = (await client.get(f"{ORGS}/{org['id']}/roles", headers=ada.headers)).json()["data"]
    assert [r["system_key"] for r in roles] == [r.key for r in SYSTEM_ROLES]
    for role, definition in zip(roles, SYSTEM_ROLES, strict=True):
        assert role["name"] == definition.name and role["is_system"] is True
        assert role["description"] == definition.description
        assert set(role["permissions"]) == definition.permissions
        assert role["member_count"] == (1 if definition.key == "owner" else 0)

    members = (await client.get(f"{ORGS}/{org['id']}/members", headers=ada.headers)).json()
    assert [m["user_id"] for m in members["data"]] == [str(ada.id)]
    assert members["data"][0]["role"]["system_key"] == "owner"

    (entry,) = await audit_entries(org["id"], "organization.created")
    assert entry["actor_user_id"] == str(ada.id) and entry["target_id"] == org["id"]
    assert entry["metadata"] == {"name": "Acme Inc", "slug": "acme-inc"}


async def test_create_uses_the_given_slug_and_timezone(client):
    ada = await create_user()
    org = await create_org(client, ada, "Acme", slug="acme-hq", timezone="Asia/Kolkata")
    assert org["slug"] == "acme-hq" and org["timezone"] == "Asia/Kolkata"


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Ünïcode Çorp", "unicode-corp"),
        ("A", "a-org"),
        ("!!!", "org"),
        ("  Spaces   & Symbols!! ", "spaces-symbols"),
        ("x" * 60, "x" * 40),
    ],
)
async def test_slug_is_derived_from_the_name(client, name, slug):
    ada = await create_user()
    assert (await create_org(client, ada, name))["slug"] == slug


async def test_derived_slug_is_made_unique_with_a_numeric_suffix(client):
    ada, grace = await create_user(), await create_user()
    slugs = [
        (await create_org(client, ada, "Acme Inc"))["slug"],
        (await create_org(client, grace, "Acme Inc"))["slug"],
        (await create_org(client, ada, "ACME inc."))["slug"],
    ]
    assert slugs == ["acme-inc", "acme-inc-2", "acme-inc-3"]
    # A long name still fits in 40 characters with its suffix.
    await create_org(client, ada, "y" * 50)
    assert (await create_org(client, ada, "y" * 50))["slug"] == "y" * 38 + "-2"


async def test_duplicate_given_slug_is_a_conflict_on_the_field(client):
    ada, grace = await create_user(), await create_user()
    await create_org(client, ada, "Acme", slug="acme")
    response = await client.post(
        ORGS, headers=grace.headers, json={"name": "Other", "slug": "acme"}
    )
    assert response.status_code == 409
    assert error(response)["code"] == "CONFLICT"
    assert error(response)["details"][0]["field"] == "slug"
    assert (await client.get(MINE, headers=grace.headers)).json()["data"] == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 121},
        {"name": "Acme", "slug": "ab"},
        {"name": "Acme", "slug": "a" * 41},
        {"name": "Acme", "slug": "-acme"},
        {"name": "Acme", "slug": "acme-"},
        {"name": "Acme", "slug": "Acme"},
        {"name": "Acme", "slug": "ac_me"},
        {"name": "Acme", "timezone": "Mars/Olympus"},
        {"name": "Acme", "storage_quota_bytes": 1},
        {"name": "Acme", "currency": "gbp"},
        {"name": "Acme", "currency": "POUND"},
        {"name": "Acme", "vat_registered": "sometimes"},
    ],
)
async def test_create_validation(client, body):
    ada = await create_user()
    response = await client.post(ORGS, headers=ada.headers, json=body)
    assert response.status_code == 422, response.text
    assert error(response)["code"] == "VALIDATION_ERROR"


async def test_a_user_may_own_at_most_ten_organizations(client):
    ada = await create_user()
    orgs = [await create_org(client, ada, f"Org {n}") for n in range(10)]
    response = await client.post(ORGS, headers=ada.headers, json={"name": "One too many"})
    assert response.status_code == 409 and error(response)["code"] == "CONFLICT"
    assert len((await client.get(MINE, headers=ada.headers)).json()["data"]) == 10
    # A deleted organization frees a slot.
    assert (await delete_org(client, ada, orgs[0])).status_code == 200
    await create_org(client, ada, "Replacement")


async def test_create_requires_a_verified_user(client):
    assert (await client.post(ORGS, json={"name": "Acme"})).status_code == 401
    unverified = await create_user(verified=False)
    response = await client.post(ORGS, headers=unverified.headers, json={"name": "Acme"})
    assert response.status_code == 403
    assert error(response)["code"] == "AUTH_EMAIL_NOT_VERIFIED"
    assert (await client.get(MINE, headers=unverified.headers)).status_code == 403


# --- list mine, by slug ---


async def test_list_my_organizations_is_ordered_and_only_active_memberships(client):
    ada = await create_user()
    zeta = await create_org(client, ada, "Zeta")
    grace, alpha = await create_user_with_org(client, "alpha")
    _, beta = await create_user_with_org(client, "Beta")
    _, gone = await create_user_with_org(client, "Gone")
    _, paused = await create_user_with_org(client, "Paused")
    await add_member(alpha["id"], "viewer", user=ada)
    await add_member(beta["id"], "admin", user=ada)
    await add_member(gone["id"], "staff", user=ada, status="removed")
    await add_member(paused["id"], "staff", user=ada, status="suspended")
    deleted = await create_org(client, ada, "Deleted")
    assert (await delete_org(client, ada, deleted)).status_code == 200

    data = (await client.get(MINE, headers=ada.headers)).json()["data"]
    assert [d["organization"]["name"] for d in data] == ["alpha", "Beta", "Zeta"]
    assert [d["role"]["system_key"] for d in data] == ["viewer", "admin", "owner"]
    assert "organization.update" not in data[0]["permissions"]
    assert "organization.update" in data[1]["permissions"]
    assert data[2]["organization"]["id"] == zeta["id"]
    assert len((await client.get(MINE, headers=grace.headers)).json()["data"]) == 1


async def test_by_slug_resolves_for_active_members_only(client):
    ada, org = await create_user_with_org(client, "Acme")
    member = await add_member(org["id"], "staff")
    suspended = await add_member(org["id"], "staff", status="suspended")
    outsider = await create_user()
    url = f"{ORGS}/by-slug/acme"

    found = await client.get(url, headers=member.headers)
    assert found.status_code == 200
    assert found.json()["data"]["organization"]["id"] == org["id"]
    assert found.json()["data"]["role"]["system_key"] == "staff"
    for actor in (outsider, suspended):
        response = await client.get(url, headers=actor.headers)
        assert response.status_code == 404 and error(response)["code"] == "RESOURCE_NOT_FOUND"
    assert (await client.get(f"{ORGS}/by-slug/nope", headers=ada.headers)).status_code == 404


# --- get ---


async def test_get_returns_the_callers_context_for_every_system_role(client):
    _, org = await create_user_with_org(client)
    for definition in SYSTEM_ROLES:
        actor = await add_member(org["id"], definition.key)
        response = await client.get(f"{ORGS}/{org['id']}", headers=actor.headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["organization"]["id"] == org["id"]
        assert data["role"]["system_key"] == definition.key
        assert set(data["permissions"]) == definition.permissions


async def test_get_is_not_found_for_outsiders_suspended_and_removed_members(client):
    _, org = await create_user_with_org(client)
    actors = [
        await create_user(),
        await add_member(org["id"], "admin", status="suspended"),
        await add_member(org["id"], "admin", status="removed"),
    ]
    for actor in actors:
        response = await client.get(f"{ORGS}/{org['id']}", headers=actor.headers)
        assert response.status_code == 404
    assert (await client.get(f"{ORGS}/{org['id']}")).status_code == 401
    assert (await client.get(f"{ORGS}/not-a-uuid", headers=actors[0].headers)).status_code == 422


# --- patch ---


async def test_patch_updates_settings_and_audits_the_changed_fields(client):
    ada, org = await create_user_with_org(client)
    body = {
        "name": "Acme Global",
        "timezone": "Europe/Berlin",
        "week_start": 7,
        "working_days": [6, 1, 2],
        "minutes_per_day": 420,
        "vat_registered": False,
    }
    response = await client.patch(f"{ORGS}/{org['id']}", headers=ada.headers, json=body)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data == {**org, **body, "working_days": [1, 2, 6]}
    assert data["slug"] == org["slug"]

    (entry,) = await audit_entries(org["id"], "organization.updated")
    assert entry["actor_user_id"] == str(ada.id) and entry["target_type"] == "organization"
    assert entry["metadata"]["changes"]["name"] == ["Acme", "Acme Global"]
    assert entry["metadata"]["changes"]["working_days"] == [[1, 2, 3, 4, 5], [1, 2, 6]]
    assert set(entry["metadata"]["changes"]) == set(body)

    # Sending the current values changes nothing and writes no audit entry.
    again = await client.patch(
        f"{ORGS}/{org['id']}", headers=ada.headers, json={"name": "Acme Global"}
    )
    assert again.status_code == 200
    assert len(await audit_entries(org["id"], "organization.updated")) == 1


async def test_patch_permission_per_system_role(client):
    _, org = await create_user_with_org(client)
    expected = {
        "owner": 200,
        "admin": 200,
        "partner": 200,
        "manager": 403,
        "staff": 403,
        "billing": 403,
        "viewer": 403,
    }
    for key, status in expected.items():
        actor = await add_member(org["id"], key)
        response = await client.patch(
            f"{ORGS}/{org['id']}", headers=actor.headers, json={"minutes_per_day": 400}
        )
        assert response.status_code == status, key
        if status == 403:
            assert error(response)["code"] == "PERMISSION_DENIED"
    outsider = await create_user()
    response = await client.patch(
        f"{ORGS}/{org['id']}", headers=outsider.headers, json={"minutes_per_day": 1}
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": None},
        {"name": "x" * 121},
        {"slug": "new-slug"},
        {"timezone": "Nowhere/City"},
        {"week_start": 0},
        {"week_start": 8},
        {"working_days": []},
        {"working_days": [1, 1]},
        {"working_days": [0, 1]},
        {"working_days": [8]},
        {"working_days": None},
        {"minutes_per_day": 0},
        {"minutes_per_day": 1441},
        {"vat_registered": "maybe"},
        {"allow_public_share_links": False},
        {"storage_quota_bytes": 10**12},
        {"currency": "EUR"},
    ],
)
async def test_patch_validation(client, body):
    ada, org = await create_user_with_org(client)
    response = await client.patch(f"{ORGS}/{org['id']}", headers=ada.headers, json=body)
    assert response.status_code == 422, response.text
    assert error(response)["details"][0]["field"] == next(iter(body))
    assert await audit_entries(org["id"], "organization.updated") == []


# --- delete ---


async def test_delete_needs_the_owner_and_the_exact_name(client):
    ada, org = await create_user_with_org(client, "Acme")
    admin = await add_member(org["id"], "admin")
    outsider = await create_user()

    assert (await delete_org(client, admin, org)).status_code == 403
    assert (await delete_org(client, outsider, org)).status_code == 404
    wrong = await delete_org(client, ada, org, confirm="acme")
    assert wrong.status_code == 422
    assert error(wrong)["details"][0]["field"] == "confirm_name"
    missing = await client.request("DELETE", f"{ORGS}/{org['id']}", headers=ada.headers, json={})
    assert missing.status_code == 422
    assert (await client.get(f"{ORGS}/{org['id']}", headers=ada.headers)).status_code == 200


async def test_delete_is_soft_hides_the_organization_and_frees_the_slug(client, db):
    from sqlalchemy import text

    ada, org = await create_user_with_org(client, "Acme")
    admin = await add_member(org["id"], "admin")
    response = await delete_org(client, ada, org)
    assert response.status_code == 200 and response.json()["data"] is None

    for actor in (ada, admin):
        assert (await client.get(f"{ORGS}/{org['id']}", headers=actor.headers)).status_code == 404
        assert (await client.get(f"{ORGS}/by-slug/acme", headers=actor.headers)).status_code == 404
        assert (await client.get(MINE, headers=actor.headers)).json()["data"] == []
    assert (await client.get(f"{ORGS}/{org['id']}/members", headers=ada.headers)).status_code == 404
    deleted_at = await db.scalar(
        text("SELECT deleted_at FROM organizations WHERE id = :id"), {"id": org["id"]}
    )
    assert deleted_at is not None
    (entry,) = await audit_entries(org["id"], "organization.deleted")
    assert entry["actor_user_id"] == str(ada.id) and entry["target_id"] == org["id"]

    assert (await create_org(client, ada, "Acme"))["slug"] == "acme"
