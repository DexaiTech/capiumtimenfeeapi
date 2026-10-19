import asyncio
import uuid

import pytest
from sqlalchemy import text

from tests.factories import (
    API,
    add_member,
    audit_entries,
    create_user,
    create_user_with_org,
    error,
    role_id,
)
from tests.foundation_helpers import add_custom_role


def members_url(org: dict, user=None) -> str:
    base = f"{API}/organizations/{org['id']}/members"
    return f"{base}/{user.id}" if user is not None else base


async def create_role(client, owner, org: dict, name: str, permissions: list[str]) -> str:
    response = await client.post(
        f"{API}/organizations/{org['id']}/roles",
        headers=owner.headers,
        json={"name": name, "permissions": permissions},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


async def member_row(db, org: dict, user) -> dict:
    row = (
        await db.execute(
            text(
                "SELECT m.status, r.system_key, r.name AS role_name, m.manager_user_id "
                "FROM organization_members m JOIN roles r ON r.id = m.role_id "
                "WHERE m.organization_id = :org AND m.user_id = :user"
            ),
            {"org": org["id"], "user": str(user.id)},
        )
    ).one()
    return dict(row._mapping)


# --- list ---


async def test_list_is_ordered_by_name_and_hides_removed_members_by_default(client):
    owner, org = await create_user_with_org(client, full_name="Mona Owner")
    await add_member(org["id"], "staff", full_name="zoe Last")
    await add_member(org["id"], "admin", full_name="Adam First", status="suspended")
    await add_member(org["id"], "staff", full_name="Rita Removed", status="removed")
    await add_member(org["id"], "viewer", full_name="bob Second")

    response = await client.get(members_url(org), headers=owner.headers)
    assert response.status_code == 200
    body = response.json()
    assert [m["full_name"] for m in body["data"]] == [
        "Adam First",
        "bob Second",
        "Mona Owner",
        "zoe Last",
    ]
    assert body["meta"] == {"next_cursor": None, "has_more": False}
    first = body["data"][0]
    assert set(first) == {
        "user_id",
        "full_name",
        "email",
        "role",
        "status",
        "manager_user_id",
        "weekly_capacity_minutes",
        "chargeable_target_bp",
        "job_title",
        "initials",
        "must_change_password",
        "joined_at",
    }
    assert first["status"] == "suspended" and first["weekly_capacity_minutes"] == 2250
    assert first["chargeable_target_bp"] == 7500 and first["initials"] is None
    assert first["role"] == {
        "id": await role_id(org["id"], "admin"),
        "name": "Admin",
        "system_key": "admin",
    }


async def test_list_filters_by_status_role_and_search(client):
    owner, org = await create_user_with_org(client, full_name="Mona Owner")
    await add_member(org["id"], "staff", full_name="Zoe Last", email="zoe@corp.example")
    await add_member(org["id"], "admin", full_name="Adam First", status="suspended")
    await add_member(org["id"], "staff", full_name="Rita Removed", status="removed")
    await add_member(org["id"], "staff", full_name="100% Real", email="percent@corp.example")
    # Another organization's members never appear.
    _, other = await create_user_with_org(client, "Other", full_name="Zoe Elsewhere")

    async def names(**params) -> list[str]:
        response = await client.get(members_url(org), headers=owner.headers, params=params)
        assert response.status_code == 200, response.text
        return [m["full_name"] for m in response.json()["data"]]

    assert await names(status="removed") == ["Rita Removed"]
    assert await names(status="suspended") == ["Adam First"]
    assert await names(status=["suspended", "removed"]) == ["Adam First", "Rita Removed"]
    assert await names(status="active") == ["100% Real", "Mona Owner", "Zoe Last"]
    assert await names(role_id=await role_id(org["id"], "staff")) == ["100% Real", "Zoe Last"]
    assert await names(role_id=await role_id(other["id"], "owner")) == []
    assert await names(q="zOE") == ["Zoe Last"]
    assert await names(q="CORP.example") == ["100% Real", "Zoe Last"]
    assert await names(q="%") == ["100% Real"]
    assert await names(q="_") == []
    assert await names(q="rita") == []
    assert await names(q="rita", status="removed") == ["Rita Removed"]
    assert await names(q="first", role_id=await role_id(org["id"], "staff")) == []

    bad = await client.get(members_url(org), headers=owner.headers, params={"status": "gone"})
    assert bad.status_code == 422
    bad = await client.get(members_url(org), headers=owner.headers, params={"role_id": "x"})
    assert bad.status_code == 422


async def test_list_is_cursor_paginated(client):
    owner, org = await create_user_with_org(client, full_name="Member 00")
    for number in range(1, 7):
        # Two pairs share a name: the user id breaks the tie, so nobody is skipped or repeated.
        await add_member(org["id"], "staff", full_name=f"Member {number // 2:02d}")
    everything = (await client.get(members_url(org), headers=owner.headers)).json()["data"]
    assert len(everything) == 7

    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        body = (await client.get(members_url(org), headers=owner.headers, params=params)).json()
        seen += [m["user_id"] for m in body["data"]]
        pages += 1
        assert body["meta"]["has_more"] is (body["meta"]["next_cursor"] is not None)
        cursor = body["meta"]["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == [m["user_id"] for m in everything]

    for params in ({"cursor": "garbage"}, {"limit": 0}, {"limit": 101}):
        response = await client.get(members_url(org), headers=owner.headers, params=params)
        assert response.status_code == 422, params


async def test_reading_members_needs_member_read(client):
    owner, org = await create_user_with_org(client)
    viewer = await add_member(org["id"], "viewer")
    await add_custom_role(org["id"], "Kiosk", ["organization.read"])
    external = await add_member(org["id"], "Kiosk")
    outsider = await create_user()

    assert (await client.get(members_url(org), headers=viewer.headers)).status_code == 200
    assert (await client.get(members_url(org, owner), headers=viewer.headers)).status_code == 200
    for url in (members_url(org), members_url(org, owner)):
        denied = await client.get(url, headers=external.headers)
        assert denied.status_code == 403 and error(denied)["code"] == "PERMISSION_DENIED"
        assert (await client.get(url, headers=outsider.headers)).status_code == 404


async def test_get_member(client):
    owner, org = await create_user_with_org(client, full_name="Mona Owner")
    outsider = await create_user()
    response = await client.get(members_url(org, owner), headers=owner.headers)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["user_id"] == str(owner.id) and data["email"] == owner.email
    assert data["full_name"] == "Mona Owner" and data["role"]["system_key"] == "owner"
    assert data["status"] == "active" and data["manager_user_id"] is None

    for user_id in (outsider.id, uuid.uuid4()):
        missing = await client.get(f"{members_url(org)}/{user_id}", headers=owner.headers)
        assert missing.status_code == 404 and error(missing)["code"] == "RESOURCE_NOT_FOUND"


# --- update: plain fields ---


async def test_update_details_and_manager(client):
    owner, org = await create_user_with_org(client)
    lead = await add_member(org["id"], "manager")
    dev = await add_member(org["id"], "staff")
    response = await client.patch(
        members_url(org, dev),
        headers=owner.headers,
        json={
            "manager_user_id": str(lead.id),
            "weekly_capacity_minutes": 1200,
            "job_title": "  Engineer  ",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["manager_user_id"] == str(lead.id)
    assert data["weekly_capacity_minutes"] == 1200 and data["job_title"] == "Engineer"
    (entry,) = await audit_entries(org["id"], "member.updated")
    assert entry["target_type"] == "member" and entry["target_id"] == str(dev.id)
    assert set(entry["metadata"]["changes"]) == {
        "manager_user_id",
        "weekly_capacity_minutes",
        "job_title",
    }

    cleared = await client.patch(
        members_url(org, dev),
        headers=owner.headers,
        json={"manager_user_id": None, "job_title": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["data"]["manager_user_id"] is None
    assert cleared.json()["data"]["job_title"] is None
    assert cleared.json()["data"]["weekly_capacity_minutes"] == 1200


async def test_a_member_may_edit_their_own_details_but_not_role_or_status(client):
    owner, org = await create_user_with_org(client)
    admin = await add_member(org["id"], "admin")
    fine = await client.patch(
        members_url(org, admin), headers=admin.headers, json={"job_title": "Ops"}
    )
    assert fine.status_code == 200 and fine.json()["data"]["job_title"] == "Ops"

    for actor in (owner, admin):
        for body in (
            {"role_id": await role_id(org["id"], "staff")},
            {"status": "suspended"},
        ):
            response = await client.patch(members_url(org, actor), headers=actor.headers, json=body)
            assert response.status_code == 403, body
            assert error(response)["code"] == "PERMISSION_DENIED"
    # Sending the values one already has is not a change.
    same = await client.patch(
        members_url(org, admin),
        headers=admin.headers,
        json={"role_id": await role_id(org["id"], "admin"), "status": "active"},
    )
    assert same.status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"role_id": None},
        {"role_id": "nope"},
        {"status": None},
        {"status": "removed"},
        {"status": "banned"},
        {"weekly_capacity_minutes": -1},
        {"weekly_capacity_minutes": 10081},
        {"weekly_capacity_minutes": None},
        {"job_title": "x" * 121},
        {"manager_user_id": "nope"},
        {"chargeable_target_bp": -1},
        {"chargeable_target_bp": 10001},
        {"chargeable_target_bp": None},
        {"chargeable_target_bp": "high"},
        {"initials": "ABCDEFG"},
        {"initials": "A.B"},
        {"initials": "Å"},
        {"email_digest": "off"},
        {"avatar_url": "https://example.com/a.png"},
        {"user_id": "5b3a4bb0-8a4f-4d5e-9d53-0d6c9f5c6f10"},
    ],
)
async def test_update_validation(client, body):
    owner, org = await create_user_with_org(client)
    dev = await add_member(org["id"], "staff")
    response = await client.patch(members_url(org, dev), headers=owner.headers, json=body)
    assert response.status_code == 422, response.text
    assert error(response)["details"][0]["field"] == next(iter(body))


async def test_update_needs_member_update(client):
    _, org = await create_user_with_org(client)
    dev = await add_member(org["id"], "staff")
    expected = {
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
            members_url(org, dev), headers=actor.headers, json={"job_title": key}
        )
        assert response.status_code == status, key
    outsider = await create_user()
    response = await client.patch(members_url(org, dev), headers=outsider.headers, json={})
    assert response.status_code == 404


# --- update: role and status rules ---


async def test_role_change_is_applied_and_audited_with_old_and_new(client, db):
    owner, org = await create_user_with_org(client)
    dev = await add_member(org["id"], "staff")
    manager_role = await role_id(org["id"], "manager")
    response = await client.patch(
        members_url(org, dev), headers=owner.headers, json={"role_id": manager_role}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["role"] == {
        "id": manager_role,
        "name": "Manager",
        "system_key": "manager",
    }
    assert (await member_row(db, org, dev))["system_key"] == "manager"
    (entry,) = await audit_entries(org["id"], "member.role_changed")
    assert entry["actor_user_id"] == str(owner.id) and entry["target_id"] == str(dev.id)
    assert entry["metadata"]["old"]["system_key"] == "staff"
    assert entry["metadata"]["new"] == {
        "role_id": manager_role,
        "role_name": "Manager",
        "system_key": "manager",
    }
    # The new role takes effect on the member's next request.
    created = await client.post(
        f"{API}/organizations/{org['id']}/roles",
        headers=dev.headers,
        json={"name": "X", "permissions": []},
    )
    assert created.status_code == 403


async def test_suspend_and_reactivate_are_audited_and_block_access(client):
    owner, org = await create_user_with_org(client)
    dev = await add_member(org["id"], "staff")
    org_url = f"{API}/organizations/{org['id']}"

    suspended = await client.patch(
        members_url(org, dev), headers=owner.headers, json={"status": "suspended"}
    )
    assert suspended.status_code == 200 and suspended.json()["data"]["status"] == "suspended"
    assert (await client.get(org_url, headers=dev.headers)).status_code == 404

    active = await client.patch(
        members_url(org, dev), headers=owner.headers, json={"status": "active"}
    )
    assert active.status_code == 200 and active.json()["data"]["status"] == "active"
    assert (await client.get(org_url, headers=dev.headers)).status_code == 200

    actions = [e["action"] for e in await audit_entries(org["id"]) if e["target_type"] == "member"]
    assert actions == ["member.suspended", "member.reactivated"]
    entry = (await audit_entries(org["id"], "member.suspended"))[0]
    assert entry["metadata"]["old"] == {"status": "active"}
    assert entry["metadata"]["new"] == {"status": "suspended"}
    assert entry["metadata"]["member_name"] and entry["metadata"]["member_email"]
    assert entry["target_id"] == str(dev.id)


async def test_the_last_active_owner_cannot_be_demoted_suspended_or_removed(client, db):
    owner, org = await create_user_with_org(client)
    admin = await add_member(org["id"], "admin")
    # A suspended second owner does not count as an active one.
    await add_member(org["id"], "owner", status="suspended")

    attempts = [
        await client.patch(
            members_url(org, owner),
            headers=admin.headers,
            json={"role_id": await role_id(org["id"], "staff")},
        ),
        await client.patch(
            members_url(org, owner), headers=admin.headers, json={"status": "suspended"}
        ),
        await client.delete(members_url(org, owner), headers=admin.headers),
        await client.delete(members_url(org, owner), headers=owner.headers),
    ]
    for response in attempts:
        assert response.status_code == 409, response.text
        assert error(response)["code"] == "CONFLICT"
        assert error(response)["details"] == {"code": "last_owner"}
    assert await member_row(db, org, owner) == {
        "status": "active",
        "system_key": "owner",
        "role_name": "Owner",
        "manager_user_id": None,
    }
    assert [e for e in await audit_entries(org["id"]) if e["target_type"] == "member"] == []


async def test_with_two_active_owners_one_can_be_demoted_suspended_or_leave(client, db):
    owner, org = await create_user_with_org(client)
    second = await add_member(org["id"], "owner")
    third = await add_member(org["id"], "owner")
    fourth = await add_member(org["id"], "owner")

    demoted = await client.patch(
        members_url(org, second),
        headers=owner.headers,
        json={"role_id": await role_id(org["id"], "admin")},
    )
    assert demoted.status_code == 200 and demoted.json()["data"]["role"]["system_key"] == "admin"
    suspended = await client.patch(
        members_url(org, third), headers=owner.headers, json={"status": "suspended"}
    )
    assert suspended.status_code == 200
    assert (
        await client.delete(members_url(org, fourth), headers=fourth.headers)
    ).status_code == 200
    # Now one active owner is left and the invariant bites again.
    last = await client.delete(members_url(org, owner), headers=owner.headers)
    assert last.status_code == 409 and error(last)["details"] == {"code": "last_owner"}


async def test_two_owners_leaving_at_the_same_time_cannot_both_succeed(client, db):
    owner, org = await create_user_with_org(client)
    second = await add_member(org["id"], "owner")
    responses = await asyncio.gather(
        client.delete(members_url(org, owner), headers=owner.headers),
        client.delete(members_url(org, second), headers=second.headers),
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    active_owners = await db.scalar(
        text(
            "SELECT count(*) FROM organization_members m JOIN roles r ON r.id = m.role_id "
            "WHERE m.organization_id = :org AND m.status = 'active' AND r.system_key = 'owner'"
        ),
        {"org": org["id"]},
    )
    assert active_owners == 1


async def test_only_an_owner_gives_or_takes_away_the_owner_role(client, db):
    owner, org = await create_user_with_org(client)
    second_owner = await add_member(org["id"], "owner")
    admin = await add_member(org["id"], "admin")
    dev = await add_member(org["id"], "staff")
    owner_role = await role_id(org["id"], "owner")
    member_role = await role_id(org["id"], "staff")

    give = await client.patch(
        members_url(org, dev), headers=admin.headers, json={"role_id": owner_role}
    )
    take = await client.patch(
        members_url(org, second_owner), headers=admin.headers, json={"role_id": member_role}
    )
    suspend = await client.patch(
        members_url(org, second_owner), headers=admin.headers, json={"status": "suspended"}
    )
    remove = await client.delete(members_url(org, second_owner), headers=admin.headers)
    for response in (give, take, suspend, remove):
        assert response.status_code == 403, response.text
        assert error(response)["code"] == "PERMISSION_DENIED"
    assert (await member_row(db, org, second_owner))["system_key"] == "owner"
    assert (await member_row(db, org, second_owner))["status"] == "active"
    assert (await member_row(db, org, dev))["system_key"] == "staff"

    promoted = await client.patch(
        members_url(org, dev), headers=owner.headers, json={"role_id": owner_role}
    )
    assert promoted.status_code == 200
    assert promoted.json()["data"]["role"]["system_key"] == "owner"


async def test_a_caller_assigns_only_roles_whose_permissions_they_hold(client, db):
    owner, org = await create_user_with_org(client)
    dev = await add_member(org["id"], "staff")
    # A custom role that may update members but holds little else.
    limited_role = await create_role(
        client, owner, org, "People Ops", ["organization.read", "member.read", "member.update"]
    )
    small_role = await create_role(client, owner, org, "Reader", ["organization.read"])
    people_ops = await add_member(org["id"], "People Ops")

    for key in ("admin", "manager", "viewer"):
        response = await client.patch(
            members_url(org, dev),
            headers=people_ops.headers,
            json={"role_id": await role_id(org["id"], key)},
        )
        assert response.status_code == 403, key
        assert error(response)["code"] == "PERMISSION_DENIED"
    assert (await member_row(db, org, dev))["system_key"] == "staff"

    for assignable in (small_role, limited_role):
        response = await client.patch(
            members_url(org, dev), headers=people_ops.headers, json={"role_id": assignable}
        )
        assert response.status_code == 200, response.text
    assert (await member_row(db, org, dev))["role_name"] == "People Ops"

    # An Admin holds everything but organization.delete: every role except Owner is assignable.
    admin = await add_member(org["id"], "admin")
    response = await client.patch(
        members_url(org, dev),
        headers=admin.headers,
        json={"role_id": await role_id(org["id"], "admin")},
    )
    assert response.status_code == 200


async def test_role_and_manager_must_belong_to_the_same_organization(client, db):
    owner, org = await create_user_with_org(client)
    dev = await add_member(org["id"], "staff")
    suspended = await add_member(org["id"], "staff", status="suspended")
    removed = await add_member(org["id"], "staff", status="removed")
    other_owner, other = await create_user_with_org(client, "Other")
    deleted_role = await create_role(client, owner, org, "Temp", [])
    assert (
        await client.delete(
            f"{API}/organizations/{org['id']}/roles/{deleted_role}", headers=owner.headers
        )
    ).status_code == 200

    bodies = [
        {"role_id": await role_id(other["id"], "staff")},
        {"role_id": str(uuid.uuid4())},
        {"role_id": deleted_role},
        {"manager_user_id": str(other_owner.id)},
        {"manager_user_id": str(uuid.uuid4())},
        {"manager_user_id": str(suspended.id)},
        {"manager_user_id": str(removed.id)},
        {"manager_user_id": str(dev.id)},
    ]
    for body in bodies:
        response = await client.patch(members_url(org, dev), headers=owner.headers, json=body)
        assert response.status_code == 422, body
        assert error(response)["code"] == "VALIDATION_ERROR"
        assert error(response)["details"][0]["field"] == next(iter(body))
    row = await member_row(db, org, dev)
    assert row["system_key"] == "staff" and row["manager_user_id"] is None


async def test_a_removed_member_cannot_be_updated(client):
    owner, org = await create_user_with_org(client)
    removed = await add_member(org["id"], "staff", status="removed")
    outsider = await create_user()
    for user in (removed, outsider):
        for body in ({"job_title": "Back"}, {"status": "active"}):
            response = await client.patch(members_url(org, user), headers=owner.headers, json=body)
            assert response.status_code == 404, body
    # The row is still readable, so the UI can show who left.
    shown = await client.get(members_url(org, removed), headers=owner.headers)
    assert shown.status_code == 200 and shown.json()["data"]["status"] == "removed"


# --- remove and leave ---


async def test_remove_keeps_the_row_ends_access_and_is_audited(client, db):
    owner, org = await create_user_with_org(client)
    lead = await add_member(org["id"], "manager")
    dev = await add_member(org["id"], "staff")
    await client.patch(
        members_url(org, dev), headers=owner.headers, json={"manager_user_id": str(lead.id)}
    )

    response = await client.delete(members_url(org, lead), headers=owner.headers)
    assert response.status_code == 200 and response.json()["data"] is None
    row = await member_row(db, org, lead)
    assert row["status"] == "removed" and row["system_key"] == "manager"
    assert (
        await client.get(f"{API}/organizations/{org['id']}", headers=lead.headers)
    ).status_code == 404
    # Nobody keeps reporting to someone who is gone.
    assert (await member_row(db, org, dev))["manager_user_id"] is None

    (entry,) = await audit_entries(org["id"], "member.removed")
    assert entry["actor_user_id"] == str(owner.id) and entry["target_id"] == str(lead.id)
    assert entry["metadata"]["old"]["status"] == "active"
    assert entry["metadata"]["old"]["system_key"] == "manager"
    assert entry["metadata"]["new"] == {"status": "removed"}
    assert entry["metadata"]["left"] is False

    again = await client.delete(members_url(org, lead), headers=owner.headers)
    assert again.status_code == 404
    listed = (await client.get(members_url(org), headers=owner.headers)).json()["data"]
    assert str(lead.id) not in [m["user_id"] for m in listed]


async def test_remove_needs_member_remove_unless_leaving(client, db):
    owner, org = await create_user_with_org(client)
    dev = await add_member(org["id"], "staff")
    for key in ("manager", "staff", "billing", "viewer"):
        actor = await add_member(org["id"], key)
        denied = await client.delete(members_url(org, dev), headers=actor.headers)
        assert denied.status_code == 403, key
        # Permission is checked before the target is looked up.
        unknown = await client.delete(f"{members_url(org)}/{uuid.uuid4()}", headers=actor.headers)
        assert unknown.status_code == 403, key
        # Anyone may leave.
        left = await client.delete(members_url(org, actor), headers=actor.headers)
        assert left.status_code == 200, key
        assert (await member_row(db, org, actor))["status"] == "removed"
        assert (
            await client.delete(members_url(org, actor), headers=actor.headers)
        ).status_code == 404

    admin = await add_member(org["id"], "admin")
    assert (await client.delete(members_url(org, dev), headers=admin.headers)).status_code == 200
    outsider = await create_user()
    assert (
        await client.delete(members_url(org, owner), headers=outsider.headers)
    ).status_code == 404
    left = [e for e in await audit_entries(org["id"], "member.removed") if e["metadata"]["left"]]
    assert len(left) == 4 and all(e["actor_user_id"] == e["target_id"] for e in left)
