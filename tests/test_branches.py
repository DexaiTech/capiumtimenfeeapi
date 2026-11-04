"""Ticket APT74: the practice's branches and the "Use branches" switch.

The branch list (create, rename, deactivate, delete, permissions, tenant isolation), the switch
(off by default, changed like every practice setting), and what it means for clients: while it is
on, creating a client, or clearing its branch, without a branch answers 422 ``BRANCH_REQUIRED``;
the list filters by branch, the export has a ``branch`` column and the import takes one."""

import csv
import io
import uuid

import pytest
from httpx import AsyncClient

from tests.clients_helpers import create_client, org_url, sql
from tests.domain_factories import make_client, practice, staff
from tests.factories import API, audit_entries, error
from tests.time_helpers import ok_data


async def create_branch(client: AsyncClient, actor, org: dict, status: int = 201, **body):
    body = {"name": "London"} | body
    response = await client.post(org_url(org, "/branches"), headers=actor.headers, json=body)
    assert response.status_code == status, response.text
    return response.json()["data"] if status < 300 else error(response)


async def use_branches(client: AsyncClient, actor, org: dict, enabled: bool = True) -> None:
    response = await client.patch(
        f"{API}/organizations/{org['id']}",
        headers=actor.headers,
        json={"branches_enabled": enabled},
    )
    assert response.status_code == 200, response.text


async def post_client(client: AsyncClient, actor, org: dict, **body):
    return await client.post(
        org_url(org, "/clients"), headers=actor.headers, json={"name": "Acme Ltd"} | body
    )


async def patch_client(client: AsyncClient, actor, org: dict, client_id: str, **body):
    return await client.patch(
        org_url(org, f"/clients/{client_id}"), headers=actor.headers, json=body
    )


async def export_rows(client: AsyncClient, actor, org: dict) -> list[dict[str, str]]:
    response = await client.get(org_url(org, "/clients/export"), headers=actor.headers)
    assert response.status_code == 200, response.text
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


# --- The branch list --------------------------------------------------------------------------


async def test_a_new_practice_has_no_branches_until_it_defines_them(client):
    owner, org = await practice(client)
    assert ok_data(await client.get(org_url(org, "/branches"), headers=owner.headers)) == []
    london = await create_branch(client, owner, org, name="  London ")
    assert london["name"] == "London" and london["is_active"] and london["can_delete"]
    assert london["client_count"] == 0
    one = ok_data(
        await client.get(org_url(org, f"/branches/{london['id']}"), headers=owner.headers)
    )
    assert one == london
    [made] = await audit_entries(org["id"], "branch.created")
    assert made["metadata"] == {"name": "London"}


async def test_names_are_unique_ignoring_case_and_validated(client):
    owner, org = await practice(client)
    await create_branch(client, owner, org, name="London")
    taken = await create_branch(client, owner, org, status=409, name=" london ")
    assert taken["code"] == "BRANCH_NAME_TAKEN"
    leeds = await create_branch(client, owner, org, name="Leeds")
    url = org_url(org, f"/branches/{leeds['id']}")
    assert (
        await client.patch(url, headers=owner.headers, json={"name": "LONDON"})
    ).status_code == 409
    renamed = await client.patch(url, headers=owner.headers, json={"name": "Leeds North"})
    assert ok_data(renamed)["name"] == "Leeds North"
    [entry] = await audit_entries(org["id"], "branch.updated")
    assert entry["metadata"]["changes"] == {"name": ["Leeds", "Leeds North"]}
    for body in ({"name": ""}, {"name": "x" * 41}, {"nope": 1}):
        response = await client.post(org_url(org, "/branches"), headers=owner.headers, json=body)
        assert response.status_code == 422, body
    for body in ({"name": None}, {"is_active": None}):
        assert (await client.patch(url, headers=owner.headers, json=body)).status_code == 422
    other_owner, other = await practice(client, "Other")
    await create_branch(client, other_owner, other, name="London")


async def test_the_list_orders_filters_and_counts_live_clients(client):
    owner, org = await practice(client)
    leeds = await create_branch(client, owner, org, name="Leeds")
    london = await create_branch(client, owner, org, name="london")
    off = await create_branch(client, owner, org, name="Bath", is_active=False)
    await make_client(org, branch_id=uuid.UUID(leeds["id"]))
    await make_client(org, branch_id=uuid.UUID(leeds["id"]))
    await make_client(org, branch_id=uuid.UUID(london["id"]))
    gone = await make_client(org, branch_id=uuid.UUID(london["id"]))
    await sql("UPDATE clients SET deleted_at = now() WHERE id = :i", i=str(gone.id))
    listed = ok_data(await client.get(org_url(org, "/branches"), headers=owner.headers))
    assert [(b["name"], b["client_count"]) for b in listed] == [
        ("Bath", 0),
        ("Leeds", 2),
        ("london", 1),
    ]
    active = ok_data(
        await client.get(org_url(org, "/branches?is_active=true"), headers=owner.headers)
    )
    assert [b["name"] for b in active] == ["Leeds", "london"]
    inactive = ok_data(
        await client.get(org_url(org, "/branches?is_active=false"), headers=owner.headers)
    )
    assert [b["id"] for b in inactive] == [off["id"]]
    found = ok_data(await client.get(org_url(org, "/branches?q=LEE"), headers=owner.headers))
    assert [b["id"] for b in found] == [leeds["id"]]


async def test_deactivate_activate_and_delete_outcomes(client):
    owner, org = await practice(client)
    used = await create_branch(client, owner, org, name="Used")
    free = await create_branch(client, owner, org, name="Free")
    once = await create_branch(client, owner, org, name="Once")
    await create_client(client, owner, org, "Acme", branch_id=used["id"])
    # A deleted client still holds its branch, so the branch is kept (deactivated).
    gone = await make_client(org, branch_id=uuid.UUID(once["id"]))
    await sql("UPDATE clients SET deleted_at = now() WHERE id = :i", i=str(gone.id))

    off = await client.post(
        org_url(org, f"/branches/{free['id']}/deactivate"), headers=owner.headers
    )
    assert ok_data(off)["is_active"] is False
    on = await client.post(org_url(org, f"/branches/{free['id']}/activate"), headers=owner.headers)
    assert ok_data(on)["is_active"] is True

    kept = await client.delete(org_url(org, f"/branches/{used['id']}"), headers=owner.headers)
    assert ok_data(kept) == {"outcome": "deactivated", "id": used["id"]}
    row = ok_data(await client.get(org_url(org, f"/branches/{used['id']}"), headers=owner.headers))
    assert row["is_active"] is False and row["can_delete"] is False
    also_kept = await client.delete(org_url(org, f"/branches/{once['id']}"), headers=owner.headers)
    assert ok_data(also_kept)["outcome"] == "deactivated"

    deleted = await client.delete(org_url(org, f"/branches/{free['id']}"), headers=owner.headers)
    assert ok_data(deleted) == {"outcome": "deleted", "id": free["id"]}
    missing = await client.get(org_url(org, f"/branches/{free['id']}"), headers=owner.headers)
    assert missing.status_code == 404
    assert [e["metadata"] for e in await audit_entries(org["id"], "branch.deleted")] == [
        {"name": "Free"}
    ]


@pytest.mark.parametrize(
    ("role", "can_write"),
    [
        ("owner", True),
        ("admin", True),
        ("partner", True),
        ("manager", False),
        ("staff", False),
        ("billing", False),
        ("viewer", False),
    ],
)
async def test_every_member_reads_and_settings_managers_write(client, role, can_write):
    owner, org = await practice(client)
    actor = owner if role == "owner" else await staff(org, role)
    existing = await create_branch(client, owner, org, name="Existing")
    assert (await client.get(org_url(org, "/branches"), headers=actor.headers)).status_code == 200
    one = await client.get(org_url(org, f"/branches/{existing['id']}"), headers=actor.headers)
    assert one.status_code == 200
    created = await client.post(
        org_url(org, "/branches"), headers=actor.headers, json={"name": "N"}
    )
    patched = await client.patch(
        org_url(org, f"/branches/{existing['id']}"), headers=actor.headers, json={"name": "E2"}
    )
    off = await client.post(
        org_url(org, f"/branches/{existing['id']}/deactivate"), headers=actor.headers
    )
    deleted = await client.delete(
        org_url(org, f"/branches/{existing['id']}"), headers=actor.headers
    )
    codes = (created.status_code, patched.status_code, off.status_code, deleted.status_code)
    assert codes == ((201, 200, 200, 200) if can_write else (403,) * 4)


async def test_other_practices_branches_are_invisible(client):
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other")
    theirs = await create_branch(client, other_owner, other, name="Theirs")
    url = org_url(org, f"/branches/{theirs['id']}")
    assert (await client.get(url, headers=owner.headers)).status_code == 404
    assert (await client.patch(url, headers=owner.headers, json={"name": "X"})).status_code == 404
    assert (await client.delete(url, headers=owner.headers)).status_code == 404
    assert ok_data(await client.get(org_url(org, "/branches"), headers=owner.headers)) == []


# --- The switch -------------------------------------------------------------------------------


async def test_branches_are_off_by_default(client):
    owner, org = await practice(client)
    assert org["branches_enabled"] is False
    fresh = await client.get(f"{API}/organizations/{org['id']}", headers=owner.headers)
    assert fresh.json()["data"]["organization"]["branches_enabled"] is False


async def test_switching_branches_on_is_saved_and_audited(client):
    owner, org = await practice(client)
    await create_branch(client, owner, org, name="London")
    url = f"{API}/organizations/{org['id']}"
    response = await client.patch(url, headers=owner.headers, json={"branches_enabled": True})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["branches_enabled"] is True
    [entry] = await audit_entries(org["id"], "organization.updated")
    assert entry["metadata"]["changes"] == {"branches_enabled": [False, True]}
    back = await client.patch(url, headers=owner.headers, json={"branches_enabled": False})
    assert back.json()["data"]["branches_enabled"] is False


async def test_branches_cannot_be_switched_on_without_an_active_branch(client):
    owner, org = await practice(client)
    url = f"{API}/organizations/{org['id']}"
    await create_branch(client, owner, org, name="Closed", is_active=False)
    refused = await client.patch(url, headers=owner.headers, json={"branches_enabled": True})
    assert refused.status_code == 422, refused.text
    problem = error(refused)
    assert problem["code"] == "NO_ACTIVE_BRANCH"
    assert problem["details"][0]["field"] == "branches_enabled"
    assert problem["details"][0]["message"] == "Add an active branch before switching branches on."
    fresh = ok_data(await client.get(url, headers=owner.headers))
    assert fresh["organization"]["branches_enabled"] is False
    assert await audit_entries(org["id"], "organization.updated") == []
    await create_branch(client, owner, org, name="London")
    await use_branches(client, owner, org)


async def test_the_last_active_branch_stays_while_branches_are_on(client):
    owner, org = await practice(client)
    london = await create_branch(client, owner, org, name="London")
    used = await create_branch(client, owner, org, name="Used")
    await create_client(client, owner, org, "Acme", branch_id=used["id"])
    await use_branches(client, owner, org)
    # Used goes (deactivated, it has a client); London is then the last active branch.
    kept = await client.delete(org_url(org, f"/branches/{used['id']}"), headers=owner.headers)
    assert ok_data(kept)["outcome"] == "deactivated"
    url = org_url(org, f"/branches/{london['id']}")
    refusals = [
        await client.post(f"{url}/deactivate", headers=owner.headers),
        await client.patch(url, headers=owner.headers, json={"is_active": False}),
        await client.delete(url, headers=owner.headers),
    ]
    for refused in refusals:
        assert refused.status_code == 409, refused.text
        assert error(refused)["code"] == "LAST_ACTIVE_BRANCH"
    assert ok_data(await client.get(url, headers=owner.headers))["is_active"] is True
    # Another active branch, or branches switched off, lets it go.
    await client.post(org_url(org, f"/branches/{used['id']}/activate"), headers=owner.headers)
    assert (await client.post(f"{url}/deactivate", headers=owner.headers)).status_code == 200
    await use_branches(client, owner, org, enabled=False)
    used_url = org_url(org, f"/branches/{used['id']}")
    assert (await client.post(f"{used_url}/deactivate", headers=owner.headers)).status_code == 200


async def test_the_switch_needs_organization_update_and_a_boolean(client):
    owner, org = await practice(client)
    member = await staff(org)
    url = f"{API}/organizations/{org['id']}"
    refused = await client.patch(url, headers=member.headers, json={"branches_enabled": True})
    assert refused.status_code == 403, refused.text
    nulled = await client.patch(url, headers=owner.headers, json={"branches_enabled": None})
    assert nulled.status_code == 422


# --- Clients ----------------------------------------------------------------------------------


async def test_switched_on_a_new_client_needs_a_branch(client):
    owner, org = await practice(client)
    london = await create_branch(client, owner, org, name="London")
    await use_branches(client, owner, org)
    for body in ({}, {"branch_id": None}):
        refused = await post_client(client, owner, org, **body)
        assert refused.status_code == 422, refused.text
        problem = error(refused)
        assert problem["code"] == "BRANCH_REQUIRED"
        assert problem["details"][0]["field"] == "branch_id"
    created = await post_client(client, owner, org, branch_id=london["id"])
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert data["branch_id"] == london["id"]
    assert data["branch"] == {"id": london["id"], "name": "London"}


async def test_switched_on_an_edit_cannot_take_the_branch_away(client):
    owner, org = await practice(client)
    london = await create_branch(client, owner, org, name="London")
    leeds = await create_branch(client, owner, org, name="Leeds")
    # Made before the practice used branches: no branch yet.
    older = await create_client(client, owner, org, "Older Ltd")
    assert older["branch_id"] is None and older["branch"] is None
    await use_branches(client, owner, org)

    acme = ok_data(await post_client(client, owner, org, branch_id=london["id"]), 201)
    cleared = await patch_client(client, owner, org, acme["id"], branch_id=None)
    assert cleared.status_code == 422 and error(cleared)["code"] == "BRANCH_REQUIRED"
    moved = await patch_client(client, owner, org, acme["id"], branch_id=leeds["id"])
    assert ok_data(moved)["branch"]["name"] == "Leeds"
    [entry] = (await audit_entries(org["id"], "client.updated"))[-1:]
    assert entry["metadata"]["changes"]["branch_id"] == [london["id"], leeds["id"]]

    # A client without a branch is not blocked: an edit that leaves the branch out still works,
    # and giving it one does too.
    phoned = await patch_client(client, owner, org, older["id"], phone="0161 000 0000")
    assert phoned.status_code == 200, phoned.text
    archived = await client.post(
        org_url(org, f"/clients/{older['id']}/archive"), headers=owner.headers
    )
    assert archived.status_code == 200, archived.text
    given = await patch_client(client, owner, org, older["id"], branch_id=london["id"])
    assert ok_data(given)["branch_id"] == london["id"]


async def test_the_branch_must_be_an_active_branch_of_the_practice(client):
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other")
    theirs = await create_branch(client, other_owner, other, name="Theirs")
    closed = await create_branch(client, owner, org, name="Closed", is_active=False)
    london = await create_branch(client, owner, org, name="London")
    await create_branch(client, owner, org, name="Leeds")
    await use_branches(client, owner, org)

    foreign = await post_client(client, owner, org, branch_id=theirs["id"])
    assert foreign.status_code == 404 and error(foreign)["details"] == {"field": "branch_id"}
    unknown = await post_client(client, owner, org, branch_id=str(uuid.uuid4()))
    assert unknown.status_code == 404
    inactive = await post_client(client, owner, org, branch_id=closed["id"])
    assert inactive.status_code == 422 and error(inactive)["details"][0]["field"] == "branch_id"

    acme = ok_data(await post_client(client, owner, org, branch_id=london["id"]), 201)
    moved = await patch_client(client, owner, org, acme["id"], branch_id=theirs["id"])
    assert moved.status_code == 404
    assert await sql("SELECT branch_id::text AS b FROM clients WHERE id = :i", i=acme["id"]) == [
        {"b": london["id"]}
    ]
    # Deactivating the branch leaves it on the client, and other edits still save.
    await client.post(org_url(org, f"/branches/{london['id']}/deactivate"), headers=owner.headers)
    kept = await patch_client(client, owner, org, acme["id"], branch_id=london["id"], notes="Hi")
    assert kept.status_code == 200, kept.text


async def test_switched_off_a_branch_is_not_required(client):
    owner, org = await practice(client)
    london = await create_branch(client, owner, org, name="London")
    plain = await post_client(client, owner, org)
    assert plain.status_code == 201, plain.text
    with_one = ok_data(
        await post_client(client, owner, org, name="Bravo", branch_id=london["id"]), 201
    )
    cleared = await patch_client(client, owner, org, with_one["id"], branch_id=None)
    assert ok_data(cleared)["branch_id"] is None
    # Switched on and off again: nothing is required any more.
    await use_branches(client, owner, org)
    await use_branches(client, owner, org, enabled=False)
    assert (await post_client(client, owner, org, name="Charlie")).status_code == 201


async def test_the_list_filters_by_branch_and_without_one(client):
    owner, org = await practice(client)
    london = await create_branch(client, owner, org, name="London")
    leeds = await create_branch(client, owner, org, name="Leeds")
    await create_client(client, owner, org, "In London", branch_id=london["id"])
    await create_client(client, owner, org, "In Leeds", branch_id=leeds["id"])
    await create_client(client, owner, org, "Nowhere")

    async def names(query: str) -> list[str]:
        rows = ok_data(await client.get(org_url(org, f"/clients{query}"), headers=owner.headers))
        return sorted(row["name"] for row in rows)

    assert await names(f"?branch_id={london['id']}") == ["In London"]
    assert await names(f"?branch_id={london['id']}&branch_id={leeds['id']}") == [
        "In Leeds",
        "In London",
    ]
    assert await names("?without_branch=true") == ["Nowhere"]
    assert await names(f"?without_branch=true&branch_id={leeds['id']}") == ["In Leeds", "Nowhere"]
    [row] = ok_data(
        await client.get(org_url(org, f"/clients?branch_id={leeds['id']}"), headers=owner.headers)
    )
    assert row["branch"] == {"id": leeds["id"], "name": "Leeds"}


async def test_the_export_has_a_branch_column_while_branches_are_on(client):
    owner, org = await practice(client)
    london = await create_branch(client, owner, org, name="London")
    await create_client(client, owner, org, "Acme Widgets Ltd", branch_id=london["id"])
    [row] = await export_rows(client, owner, org)
    assert "branch" not in row and row["name"] == "Acme Widgets Ltd"
    await use_branches(client, owner, org)
    [row] = await export_rows(client, owner, org)
    assert row["branch"] == "London"


async def test_the_import_takes_a_branch_and_needs_one_while_branches_are_on(client):
    owner, org = await practice(client)
    london = await create_branch(client, owner, org, name="London")
    await create_branch(client, owner, org, name="Closed", is_active=False)
    await use_branches(client, owner, org)
    content = "\r\n".join(
        ["Client Name,Branch", "Acme Ltd,london", "Bravo Ltd,", "Charlie Ltd,Closed", "Delta,Paris"]
    ).encode()

    async def upload(body: bytes, dry_run: bool):
        query = "?dry_run=true" if dry_run else ""
        return await client.post(
            org_url(org, f"/imports/clients{query}"),
            headers=owner.headers,
            files={"file": ("clients.csv", body, "text/csv")},
        )

    data = ok_data(await upload(content, dry_run=True))
    assert "branch" in data["columns"]
    assert {(e["row"], e["field"], e["code"]) for e in data["errors"]} == {
        (2, "branch", "BRANCH_REQUIRED"),
        (3, "branch", "unknown_branch"),
        (4, "branch", "unknown_branch"),
    }
    done = await upload(b"Client Name,Branch\r\nAcme Ltd,london\r\n", dry_run=False)
    assert done.status_code == 200, done.text
    assert await sql(
        "SELECT branch_id::text AS b FROM clients WHERE organization_id = :o AND NOT is_internal",
        o=org["id"],
    ) == [{"b": london["id"]}]


async def test_converting_a_lead_needs_a_branch_while_branches_are_on(client):
    owner, org = await practice(client)
    london = await create_branch(client, owner, org, name="London")
    await use_branches(client, owner, org)
    created = await client.post(
        org_url(org, "/leads"), headers=owner.headers, json={"name": "Bloggs Bakery Ltd"}
    )
    lead = ok_data(created, 201)
    url = org_url(org, f"/leads/{lead['id']}/convert")
    refused = await client.post(url, headers=owner.headers, json={})
    assert refused.status_code == 422 and error(refused)["code"] == "BRANCH_REQUIRED"
    converted = await client.post(url, headers=owner.headers, json={"branch_id": london["id"]})
    assert converted.status_code == 200, converted.text
    client_id = ok_data(converted)["converted_client"]["id"]
    assert await sql("SELECT branch_id::text AS b FROM clients WHERE id = :i", i=client_id) == [
        {"b": london["id"]}
    ]


# --- Bulk "Set branch" ------------------------------------------------------------------------


async def bulk_set_branch(client: AsyncClient, actor, org: dict, ids, value, status: int = 200):
    response = await client.post(
        org_url(org, "/clients/bulk"),
        headers=actor.headers,
        json={"client_ids": [str(i) for i in ids], "action": "set_branch", "value": value},
    )
    assert response.status_code == status, response.text
    return ok_data(response) if status == 200 else error(response)


async def branch_of(client_id) -> str | None:
    [row] = await sql("SELECT branch_id::text AS b FROM clients WHERE id = :i", i=str(client_id))
    return row["b"]


async def test_bulk_set_branch_gives_the_clients_without_one_a_branch(client):
    owner, org = await practice(client)
    london = await create_branch(client, owner, org, name="London")
    leeds = await create_branch(client, owner, org, name="Leeds")
    rows = [await make_client(org, f"Client {n} Ltd") for n in range(3)]
    ids = [row.id for row in rows]
    await use_branches(client, owner, org)

    result = await bulk_set_branch(client, owner, org, ids[:2], london["id"])
    assert result == {"action": "set_branch", "affected": 2, "changed": 2}
    assert [await branch_of(i) for i in ids] == [london["id"], london["id"], None]
    entries = await audit_entries(org["id"], "client.updated")
    assert len(entries) == 2
    assert {e["target_id"] for e in entries} == {str(i) for i in ids[:2]}
    assert entries[0]["metadata"]["bulk"] is True
    assert entries[0]["metadata"]["changes"] == {"branch_id": [None, london["id"]]}

    # A client already in the branch does not change; the others move.
    again = await bulk_set_branch(client, owner, org, ids, leeds["id"])
    assert again["changed"] == 3
    same = await bulk_set_branch(client, owner, org, ids, leeds["id"])
    assert same["changed"] == 0
    listed = ok_data(
        await client.get(
            org_url(org, "/clients"), headers=owner.headers, params={"without_branch": "true"}
        )
    )
    assert listed == []


async def test_bulk_set_branch_refuses_foreign_unknown_and_inactive_branches(client):
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other")
    theirs = await create_branch(client, other_owner, other, name="Theirs")
    closed = await create_branch(client, owner, org, name="Closed", is_active=False)
    await create_branch(client, owner, org, name="London")
    ids = [(await make_client(org, "Acme Ltd")).id]
    await use_branches(client, owner, org)

    foreign = await bulk_set_branch(client, owner, org, ids, theirs["id"], 404)
    assert foreign["details"] == {"field": "branch_id"}
    await bulk_set_branch(client, owner, org, ids, str(uuid.uuid4()), 404)
    inactive = await bulk_set_branch(client, owner, org, ids, closed["id"], 422)
    assert inactive["details"][0]["field"] == "branch_id"
    # A branch is required and must be an id.
    missing = await bulk_set_branch(client, owner, org, ids, None, 422)
    assert missing["details"][0]["field"] == "value"
    await bulk_set_branch(client, owner, org, ids, "London", 422)
    assert await branch_of(ids[0]) is None
    assert await audit_entries(org["id"], "client.updated") == []


async def test_bulk_set_branch_needs_the_permission_of_bulk_set_segment(client):
    owner, org = await practice(client)
    london = await create_branch(client, owner, org, name="London")
    ids = [(await make_client(org, "Acme Ltd")).id]
    plain = await staff(org, "staff")  # no client.update: bulk set_segment is 403 too
    manager = await staff(org, "manager")  # client.update

    for action, value in (("set_segment", "Gold"), ("set_branch", london["id"])):
        response = await client.post(
            org_url(org, "/clients/bulk"),
            headers=plain.headers,
            json={"client_ids": [str(ids[0])], "action": action, "value": value},
        )
        assert response.status_code == 403, (action, response.text)
    assert await branch_of(ids[0]) is None
    done = await bulk_set_branch(client, manager, org, ids, london["id"])
    assert done["changed"] == 1
