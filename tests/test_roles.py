import uuid

import pytest
from sqlalchemy import text

from app.core.permissions import ALL_CODES, CATALOGUE, SYSTEM_ROLES
from tests.factories import (
    API,
    add_member,
    audit_entries,
    create_user,
    create_user_with_org,
    error,
    role_id,
)

BASIC = ["organization.read", "role.read", "role.manage"]
# System roles without role.manage (and, for the last three, without role.read).
NOT_ROLE_MANAGERS = ("partner", "manager", "staff", "billing", "viewer")


def roles_url(org: dict, role: str | None = None) -> str:
    base = f"{API}/organizations/{org['id']}/roles"
    return f"{base}/{role}" if role else base


async def create_role(client, actor, org: dict, name: str = "Support", permissions=None, **extra):
    return await client.post(
        roles_url(org),
        headers=actor.headers,
        json={"name": name, "permissions": BASIC if permissions is None else permissions, **extra},
    )


# --- catalogue ---


async def test_permission_catalogue_is_ordered_by_category_then_code(client):
    user = await create_user()
    response = await client.get(f"{API}/permissions", headers=user.headers)
    assert response.status_code == 200
    data = response.json()["data"]
    assert [(p["category"], p["code"]) for p in data] == sorted(
        (p.category, p.code) for p in CATALOGUE
    )
    assert {p["code"]: p["description"] for p in data} == {p.code: p.description for p in CATALOGUE}
    assert not any(p["code"].startswith("page.") for p in data)
    assert (await client.get(f"{API}/permissions")).status_code == 401
    unverified = await create_user(verified=False)
    assert (await client.get(f"{API}/permissions", headers=unverified.headers)).status_code == 403


# --- list ---


async def test_list_puts_system_roles_first_then_custom_by_name_with_member_counts(client):
    owner, org = await create_user_with_org(client)
    for name in ("zeta", "Alpha", "beta"):
        assert (await create_role(client, owner, org, name)).status_code == 201
    await add_member(org["id"], "staff")
    await add_member(org["id"], "staff", status="suspended")
    await add_member(org["id"], "staff", status="removed")
    await add_member(org["id"], "beta")

    response = await client.get(roles_url(org), headers=owner.headers)
    assert response.status_code == 200
    data = response.json()["data"]
    assert [r["name"] for r in data] == [
        "Owner",
        "Admin",
        "Partner",
        "Manager",
        "Staff",
        "Billing",
        "Viewer",
        "Alpha",
        "beta",
        "zeta",
    ]
    assert [r["system_key"] for r in data[:7]] == [r.key for r in SYSTEM_ROLES]
    counts = {r["name"]: r["member_count"] for r in data}
    assert counts == {**dict.fromkeys(counts, 0), "Owner": 1, "Staff": 2, "beta": 1}
    custom = data[-1]
    assert custom["system_key"] is None and custom["is_system"] is False
    assert custom["permissions"] == sorted(BASIC) and custom["description"] is None
    assert response.json()["meta"] is None


async def test_system_roles_hold_their_documented_permissions(client):
    owner, org = await create_user_with_org(client)
    roles = {
        r["system_key"]: r
        for r in (await client.get(roles_url(org), headers=owner.headers)).json()["data"]
    }
    assert set(roles["owner"]["permissions"]) == ALL_CODES
    assert set(roles["admin"]["permissions"]) == ALL_CODES - {"organization.delete"}
    assert "role.manage" not in roles["partner"]["permissions"]
    assert "wip.write_off" in roles["partner"]["permissions"]
    for key in ("manager", "staff", "billing", "viewer"):
        assert "member.update" not in roles[key]["permissions"], key
        assert "organization.update" not in roles[key]["permissions"], key
    # Cost rates are sensitive: only the senior roles and managers read them.
    assert [k for k, r in roles.items() if "rate.read" in r["permissions"]] == [
        "owner",
        "admin",
        "partner",
        "manager",
    ]
    for role in roles.values():
        assert role["is_system"] is True and role["description"]


async def test_list_needs_role_read(client):
    _, org = await create_user_with_org(client)
    expected = {
        "admin": 200,
        "partner": 200,
        "manager": 200,
        "staff": 403,
        "billing": 403,
        "viewer": 403,
    }
    for key, status in expected.items():
        actor = await add_member(org["id"], key)
        assert (await client.get(roles_url(org), headers=actor.headers)).status_code == status, key
    outsider = await create_user()
    assert (await client.get(roles_url(org), headers=outsider.headers)).status_code == 404


# --- create ---


async def test_create_role(client):
    owner, org = await create_user_with_org(client)
    response = await create_role(
        client,
        owner,
        org,
        "  Support  ",
        ["role.read", "organization.read", "role.read"],
        description="Helps customers",
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["name"] == "Support" and data["description"] == "Helps customers"
    assert data["permissions"] == ["organization.read", "role.read"]
    assert data["system_key"] is None and data["is_system"] is False
    assert data["member_count"] == 0

    (entry,) = await audit_entries(org["id"], "role.created")
    assert entry["actor_user_id"] == str(owner.id)
    assert entry["target_type"] == "role" and entry["target_id"] == data["id"]
    assert entry["metadata"] == {
        "name": "Support",
        "permissions": ["organization.read", "role.read"],
    }
    # A role may start empty.
    empty = await create_role(client, owner, org, "Empty", [])
    assert empty.status_code == 201 and empty.json()["data"]["permissions"] == []


async def test_create_rejects_duplicate_names_case_insensitively(client):
    owner, org = await create_user_with_org(client)
    assert (await create_role(client, owner, org, "Support")).status_code == 201
    for name in ("support", "SUPPORT", "Owner", "admin", "partner", "BILLING"):
        response = await create_role(client, owner, org, name)
        assert response.status_code == 409, name
        assert error(response)["code"] == "CONFLICT"
        assert error(response)["details"][0]["field"] == "name"
    # The same name is free in another practice.
    other_owner, other = await create_user_with_org(client, "Other")
    assert (await create_role(client, other_owner, other, "Support")).status_code == 201


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"permissions": []}, "name"),
        ({"name": "", "permissions": []}, "name"),
        ({"name": "x" * 61, "permissions": []}, "name"),
        ({"name": "Ok"}, "permissions"),
        ({"name": "Ok", "permissions": "role.read"}, "permissions"),
        ({"name": "Ok", "permissions": ["role.read", "does.not_exist"]}, "permissions"),
        ({"name": "Ok", "permissions": ["page.dashboard"]}, "permissions"),
        ({"name": "Ok", "permissions": [], "description": "x" * 301}, "description"),
        ({"name": "Ok", "permissions": [], "system_key": "owner"}, "system_key"),
    ],
)
async def test_create_validation(client, body, field):
    owner, org = await create_user_with_org(client)
    response = await client.post(roles_url(org), headers=owner.headers, json=body)
    assert response.status_code == 422, response.text
    assert error(response)["code"] == "VALIDATION_ERROR"
    assert error(response)["details"][0]["field"] == field
    assert await audit_entries(org["id"], "role.created") == []


async def test_create_needs_role_manage_and_every_granted_permission(client):
    owner, org = await create_user_with_org(client)
    for key in NOT_ROLE_MANAGERS:
        actor = await add_member(org["id"], key)
        assert (await create_role(client, actor, org, key + " role", [])).status_code == 403, key

    # An Admin lacks organization.delete and so cannot put it in a role.
    admin = await add_member(org["id"], "admin")
    denied = await create_role(client, admin, org, "Destroyer", ["organization.delete"])
    assert denied.status_code == 403 and error(denied)["code"] == "PERMISSION_DENIED"
    assert (await create_role(client, admin, org, "Helper", ["member.invite"])).status_code == 201

    # A custom role manager holds only three permissions.
    assert (await create_role(client, owner, org, "Role Admin", BASIC)).status_code == 201
    role_admin = await add_member(org["id"], "Role Admin")
    assert (await create_role(client, role_admin, org, "Same", BASIC)).status_code == 201
    denied = await create_role(client, role_admin, org, "More", [*BASIC, "member.read"])
    assert denied.status_code == 403
    names = [
        r["name"] for r in (await client.get(roles_url(org), headers=owner.headers)).json()["data"]
    ]
    assert "More" not in names and "Destroyer" not in names


async def test_another_practices_roles_are_404(client):
    owner, org = await create_user_with_org(client)
    other_owner, other = await create_user_with_org(client, "Other")
    foreign = (await create_role(client, other_owner, other, "Theirs")).json()["data"]
    # The id exists, but in another practice: indistinguishable from an unknown id.
    attempts = [
        await client.patch(
            roles_url(org, foreign["id"]), headers=owner.headers, json={"name": "X"}
        ),
        await client.put(
            roles_url(org, foreign["id"]) + "/permissions",
            headers=owner.headers,
            json={"permissions": []},
        ),
        await client.delete(roles_url(org, foreign["id"]), headers=owner.headers),
    ]
    assert [r.status_code for r in attempts] == [404, 404, 404]
    # And a practice the caller does not belong to is 404 for every route.
    assert (await client.get(roles_url(other), headers=owner.headers)).status_code == 404
    assert (await create_role(client, owner, other, "Mine")).status_code == 404


# --- update ---


async def test_rename_and_describe_a_custom_role(client):
    owner, org = await create_user_with_org(client)
    role = (await create_role(client, owner, org, "Support", description="Old")).json()["data"]
    await add_member(org["id"], "Support")

    response = await client.patch(
        roles_url(org, role["id"]),
        headers=owner.headers,
        json={"name": "Customer Care", "description": None},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["name"] == "Customer Care" and data["description"] is None
    assert data["permissions"] == sorted(BASIC) and data["member_count"] == 1
    (entry,) = await audit_entries(org["id"], "role.updated")
    assert entry["target_id"] == role["id"]
    assert entry["metadata"]["changes"] == {
        "name": ["Support", "Customer Care"],
        "description": ["Old", None],
    }

    # Changing only the case of its own name is allowed; taking another role's name is not.
    recase = await client.patch(
        roles_url(org, role["id"]), headers=owner.headers, json={"name": "customer care"}
    )
    assert recase.status_code == 200
    for name in ("Staff", "OWNER"):
        clash = await client.patch(
            roles_url(org, role["id"]), headers=owner.headers, json={"name": name}
        )
        assert clash.status_code == 409 and error(clash)["details"][0]["field"] == "name"
    for body in ({"name": None}, {"name": ""}, {"name": "x" * 61}, {"permissions": []}):
        invalid = await client.patch(roles_url(org, role["id"]), headers=owner.headers, json=body)
        assert invalid.status_code == 422, body
    missing = await client.patch(
        roles_url(org, str(uuid.uuid4())), headers=owner.headers, json={"name": "X"}
    )
    assert missing.status_code == 404


async def test_system_roles_are_immutable(client):
    owner, org = await create_user_with_org(client)
    for definition in SYSTEM_ROLES:
        system_role = await role_id(org["id"], definition.key)
        attempts = [
            await client.patch(
                roles_url(org, system_role), headers=owner.headers, json={"name": "Renamed"}
            ),
            await client.patch(
                roles_url(org, system_role), headers=owner.headers, json={"description": "x"}
            ),
            await client.put(
                roles_url(org, system_role) + "/permissions",
                headers=owner.headers,
                json={"permissions": []},
            ),
            await client.delete(roles_url(org, system_role), headers=owner.headers),
        ]
        for response in attempts:
            assert response.status_code == 409, response.text
            assert error(response)["code"] == "CONFLICT"
    roles = (await client.get(roles_url(org), headers=owner.headers)).json()["data"]
    for role, definition in zip(roles, SYSTEM_ROLES, strict=True):
        assert role["name"] == definition.name
        assert set(role["permissions"]) == definition.permissions
    assert [e for e in await audit_entries(org["id"]) if e["target_type"] == "role"] == []


# --- permissions ---


async def test_replace_permissions(client):
    owner, org = await create_user_with_org(client)
    role = (await create_role(client, owner, org, "Support")).json()["data"]
    holder = await add_member(org["id"], "Support")
    url = roles_url(org, role["id"]) + "/permissions"

    response = await client.put(
        url, headers=owner.headers, json={"permissions": ["organization.read", "member.read"]}
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["permissions"] == ["member.read", "organization.read"]
    assert data["member_count"] == 1 and data["name"] == "Support"
    (entry,) = await audit_entries(org["id"], "role.permissions_changed")
    assert entry["target_id"] == role["id"]
    assert entry["metadata"]["added"] == ["member.read"]
    assert entry["metadata"]["removed"] == ["role.manage", "role.read"]

    # The new set applies to holders at once.
    context = await client.get(f"{API}/organizations/{org['id']}", headers=holder.headers)
    assert context.json()["data"]["permissions"] == ["member.read", "organization.read"]
    assert (await client.get(roles_url(org), headers=holder.headers)).status_code == 403

    unknown = await client.put(url, headers=owner.headers, json={"permissions": ["nope.nope"]})
    assert unknown.status_code == 422 and error(unknown)["details"][0]["field"] == "permissions"
    assert (await client.put(url, headers=owner.headers, json={})).status_code == 422
    missing = await client.put(
        roles_url(org, str(uuid.uuid4())) + "/permissions",
        headers=owner.headers,
        json={"permissions": []},
    )
    assert missing.status_code == 404
    emptied = await client.put(url, headers=owner.headers, json={"permissions": []})
    assert emptied.status_code == 200 and emptied.json()["data"]["permissions"] == []
    # Sending the same set again is not a change and writes no audit entry.
    again = await client.put(url, headers=owner.headers, json={"permissions": []})
    assert again.status_code == 200
    assert len(await audit_entries(org["id"], "role.permissions_changed")) == 2


async def test_replacing_permissions_needs_every_old_and_new_permission(client):
    owner, org = await create_user_with_org(client)
    admin = await add_member(org["id"], "admin")
    powerful = (
        await create_role(
            client, owner, org, "Closer", ["organization.read", "organization.delete"]
        )
    ).json()["data"]
    plain = (await create_role(client, owner, org, "Plain", ["organization.read"])).json()["data"]

    # The old set holds a permission the Admin lacks: even removing it is refused.
    strip = await client.put(
        roles_url(org, powerful["id"]) + "/permissions",
        headers=admin.headers,
        json={"permissions": ["organization.read"]},
    )
    # The new set holds a permission the Admin lacks.
    escalate = await client.put(
        roles_url(org, plain["id"]) + "/permissions",
        headers=admin.headers,
        json={"permissions": ["organization.read", "organization.delete"]},
    )
    for response in (strip, escalate):
        assert response.status_code == 403, response.text
        assert error(response)["code"] == "PERMISSION_DENIED"
    # Renaming or deleting a role one could not have built is refused as well.
    rename = await client.patch(
        roles_url(org, powerful["id"]), headers=admin.headers, json={"name": "Mine"}
    )
    assert rename.status_code == 403
    assert (
        await client.delete(roles_url(org, powerful["id"]), headers=admin.headers)
    ).status_code == 403

    allowed = await client.put(
        roles_url(org, plain["id"]) + "/permissions",
        headers=admin.headers,
        json={"permissions": ["organization.read", "member.invite"]},
    )
    assert allowed.status_code == 200
    roles = {
        r["name"]: r
        for r in (await client.get(roles_url(org), headers=owner.headers)).json()["data"]
    }
    assert roles["Closer"]["permissions"] == ["organization.delete", "organization.read"]
    assert roles["Plain"]["permissions"] == ["member.invite", "organization.read"]
    assert len(await audit_entries(org["id"], "role.permissions_changed")) == 1


async def test_changing_roles_needs_role_manage(client):
    owner, org = await create_user_with_org(client)
    role = (await create_role(client, owner, org, "Support", [])).json()["data"]
    for key in NOT_ROLE_MANAGERS:
        actor = await add_member(org["id"], key)
        attempts = [
            await client.patch(
                roles_url(org, role["id"]), headers=actor.headers, json={"name": "X"}
            ),
            await client.put(
                roles_url(org, role["id"]) + "/permissions",
                headers=actor.headers,
                json={"permissions": []},
            ),
            await client.delete(roles_url(org, role["id"]), headers=actor.headers),
        ]
        assert [r.status_code for r in attempts] == [403, 403, 403], key


# --- delete ---


async def test_delete_is_soft_and_frees_the_name(client, db):
    owner, org = await create_user_with_org(client)
    role = (await create_role(client, owner, org, "Support")).json()["data"]
    response = await client.delete(roles_url(org, role["id"]), headers=owner.headers)
    assert response.status_code == 200 and response.json()["data"] is None

    deleted_at = await db.scalar(
        text("SELECT deleted_at FROM roles WHERE id = :id"), {"id": role["id"]}
    )
    assert deleted_at is not None
    names = [
        r["name"] for r in (await client.get(roles_url(org), headers=owner.headers)).json()["data"]
    ]
    assert "Support" not in names
    (entry,) = await audit_entries(org["id"], "role.deleted")
    assert entry["target_id"] == role["id"] and entry["metadata"] == {"name": "Support"}

    # A deleted role is gone for every endpoint, and its name can be used again.
    assert (
        await client.delete(roles_url(org, role["id"]), headers=owner.headers)
    ).status_code == 404
    gone = await client.patch(roles_url(org, role["id"]), headers=owner.headers, json={"name": "X"})
    assert gone.status_code == 404
    assert (await create_role(client, owner, org, "Support")).status_code == 201


async def test_a_role_with_members_cannot_be_deleted(client):
    owner, org = await create_user_with_org(client)
    role = (await create_role(client, owner, org, "Support")).json()["data"]
    url = roles_url(org, role["id"])

    holder = await add_member(org["id"], "Support", status="suspended")
    in_use = await client.delete(url, headers=owner.headers)
    assert in_use.status_code == 409 and error(in_use)["code"] == "CONFLICT"
    assert error(in_use)["details"] == {"code": "role_in_use"}
    assert await audit_entries(org["id"], "role.deleted") == []

    # Once the member is removed the row still points at the role, but no longer blocks it.
    members = f"{API}/organizations/{org['id']}/members/{holder.id}"
    assert (await client.delete(members, headers=owner.headers)).status_code == 200
    assert (await client.delete(url, headers=owner.headers)).status_code == 200
