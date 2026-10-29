"""Client segments: the practice's list, renaming, deactivating, deleting, the staff who look
after them, permissions and tenant isolation; and the migration of the free-text values."""

import subprocess
import sys
import uuid

import pytest
from httpx import AsyncClient

from tests.clients_helpers import create_client, org_url, scalar, sql
from tests.conftest import ROOT
from tests.domain_factories import make_client, practice, staff
from tests.factories import audit_entries, error
from tests.team_helpers import new_staff
from tests.time_helpers import ok_data


async def create_segment(client: AsyncClient, actor, org: dict, status: int = 201, **body):
    body = {"name": "Gold"} | body
    response = await client.post(org_url(org, "/segments"), headers=actor.headers, json=body)
    assert response.status_code == status, response.text
    return response.json()["data"] if status < 300 else error(response)


async def test_a_new_practice_has_no_segments_until_it_defines_them(client):
    owner, org = await practice(client)
    assert ok_data(await client.get(org_url(org, "/segments"), headers=owner.headers)) == []
    gold = await create_segment(client, owner, org, description="  Top fee payers ")
    assert gold["name"] == "Gold" and gold["description"] == "Top fee payers"
    assert gold["is_active"] and gold["can_delete"]
    assert (gold["client_count"], gold["staff_count"]) == (0, 0)
    one = ok_data(await client.get(org_url(org, f"/segments/{gold['id']}"), headers=owner.headers))
    assert one == gold
    [made] = await audit_entries(org["id"], "segment.created")
    assert made["metadata"] == {"name": "Gold"}


async def test_names_are_unique_ignoring_case_and_validated(client):
    owner, org = await practice(client)
    await create_segment(client, owner, org, name="Gold")
    taken = await create_segment(client, owner, org, status=409, name=" gold ")
    assert taken["code"] == "SEGMENT_NAME_TAKEN"
    silver = await create_segment(client, owner, org, name="Silver")
    url = org_url(org, f"/segments/{silver['id']}")
    response = await client.patch(url, headers=owner.headers, json={"name": "GOLD"})
    assert response.status_code == 409
    assert (
        await client.patch(url, headers=owner.headers, json={"name": "SILVER"})
    ).status_code == 200
    for body in ({"name": ""}, {"name": "x" * 41}, {"name": "A", "sort_order": -1}, {"nope": 1}):
        response = await client.post(org_url(org, "/segments"), headers=owner.headers, json=body)
        assert response.status_code == 422, body
    for body in ({"name": None}, {"is_active": None}):
        assert (await client.patch(url, headers=owner.headers, json=body)).status_code == 422
    other_owner, other = await practice(client, "Other")
    await create_segment(client, other_owner, other, name="Gold")


async def test_the_list_orders_filters_and_counts_clients_ignoring_case(client):
    owner, org = await practice(client)
    await create_segment(client, owner, org, name="Silver", sort_order=2)
    gold = await create_segment(client, owner, org, name="Gold", sort_order=1)
    off = await create_segment(client, owner, org, name="Retired", sort_order=3, is_active=False)
    await make_client(org, segment="gold")
    await make_client(org, segment=" GOLD ")
    await make_client(org, segment="Silver")
    await make_client(org, segment="Gold", deleted_at=None)
    deleted = await make_client(org, segment="Gold")
    await sql("UPDATE clients SET deleted_at = now() WHERE id = :i", i=str(deleted.id))
    listed = ok_data(await client.get(org_url(org, "/segments"), headers=owner.headers))
    assert [(s["name"], s["client_count"]) for s in listed] == [
        ("Gold", 3),
        ("Silver", 1),
        ("Retired", 0),
    ]
    active = ok_data(
        await client.get(org_url(org, "/segments?is_active=true"), headers=owner.headers)
    )
    assert [s["name"] for s in active] == ["Gold", "Silver"]
    inactive = ok_data(
        await client.get(org_url(org, "/segments?is_active=false"), headers=owner.headers)
    )
    assert [s["id"] for s in inactive] == [off["id"]]
    found = ok_data(await client.get(org_url(org, "/segments?q=gol"), headers=owner.headers))
    assert [s["id"] for s in found] == [gold["id"]]


async def test_renaming_a_segment_renames_it_on_its_clients(client):
    owner, org = await practice(client)
    gold = await create_segment(client, owner, org, name="Gold")
    acme = await create_client(client, owner, org, "Acme", segment="gold")
    other = await create_client(client, owner, org, "Other", segment="Silver")
    response = await client.patch(
        org_url(org, f"/segments/{gold['id']}"), headers=owner.headers, json={"name": "Platinum"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["client_count"] == 1
    assert await scalar("SELECT segment FROM clients WHERE id = :i", i=acme["id"]) == "Platinum"
    assert await scalar("SELECT segment FROM clients WHERE id = :i", i=other["id"]) == "Silver"
    [entry] = await audit_entries(org["id"], "segment.updated")
    assert entry["metadata"]["clients_renamed"] == 1
    assert entry["metadata"]["changes"] == {"name": ["Gold", "Platinum"]}


async def test_deactivate_activate_and_delete_outcomes(client):
    owner, org = await practice(client)
    used = await create_segment(client, owner, org, name="Used")
    free = await create_segment(client, owner, org, name="Free")
    await create_client(client, owner, org, "Acme", segment="Used")
    assert not (await create_segment(client, owner, org, name="Spare"))["client_count"]
    off = await client.post(
        org_url(org, f"/segments/{free['id']}/deactivate"), headers=owner.headers
    )
    assert ok_data(off)["is_active"] is False
    on = await client.post(org_url(org, f"/segments/{free['id']}/activate"), headers=owner.headers)
    assert ok_data(on)["is_active"] is True
    kept = await client.delete(org_url(org, f"/segments/{used['id']}"), headers=owner.headers)
    assert ok_data(kept) == {"outcome": "deactivated", "id": used["id"]}
    row = ok_data(await client.get(org_url(org, f"/segments/{used['id']}"), headers=owner.headers))
    assert row["is_active"] is False and row["can_delete"] is False
    gone = await client.delete(org_url(org, f"/segments/{free['id']}"), headers=owner.headers)
    assert ok_data(gone) == {"outcome": "deleted", "id": free["id"]}
    missing = await client.get(org_url(org, f"/segments/{free['id']}"), headers=owner.headers)
    assert missing.status_code == 404
    assert [e["metadata"] for e in await audit_entries(org["id"], "segment.deleted")] == [
        {"name": "Free"}
    ]


async def test_staff_look_after_segments_and_see_their_clients(client):
    owner, org = await practice(client)
    gold = await create_segment(client, owner, org, name="Gold")
    silver = await create_segment(client, owner, org, name="Silver")
    retired = await create_segment(client, owner, org, name="Retired", is_active=False)
    member = await new_staff(client, owner, org, "Sam Staff")
    url = org_url(org, f"/staff/{member['id']}/segments")
    assert ok_data(await client.get(url, headers=owner.headers)) == {
        "segments": [],
        "client_count": 0,
    }
    for name in ("gold", "SILVER", "Gold"):
        await make_client(org, segment=name)
    await make_client(org, segment="Bronze")
    response = await client.put(
        url, headers=owner.headers, json={"segment_ids": [gold["id"], silver["id"], gold["id"]]}
    )
    data = ok_data(response)
    assert [s["name"] for s in data["segments"]] == ["Gold", "Silver"]
    assert data["client_count"] == 3
    assert data["segments"][0]["staff_count"] == 1
    # Replacing the set removes what is left out.
    data = ok_data(
        await client.put(url, headers=owner.headers, json={"segment_ids": [silver["id"]]})
    )
    assert [s["name"] for s in data["segments"]] == ["Silver"] and data["client_count"] == 1
    assert (
        await scalar("SELECT count(*) FROM staff_segments WHERE organization_id = :o", o=org["id"])
        == 1
    )
    [entry] = (await audit_entries(org["id"], "staff.segments_updated"))[-1:]
    assert entry["metadata"] == {"added": [], "removed": ["Gold"]}
    # An inactive segment cannot be added, and an unknown one does not exist.
    bad = await client.put(url, headers=owner.headers, json={"segment_ids": [retired["id"]]})
    assert bad.status_code == 422
    unknown = await client.put(
        url, headers=owner.headers, json={"segment_ids": [str(uuid.uuid4())]}
    )
    assert unknown.status_code == 422
    # Deactivating a segment a person has leaves it with them.
    await client.post(org_url(org, f"/segments/{silver['id']}/deactivate"), headers=owner.headers)
    kept = await client.put(url, headers=owner.headers, json={"segment_ids": [silver["id"]]})
    assert kept.status_code == 200
    # Deleting a segment takes it off the staff.
    await client.delete(org_url(org, f"/segments/{gold['id']}"), headers=owner.headers)
    missing = await client.get(
        org_url(org, f"/staff/{uuid.uuid4()}/segments"), headers=owner.headers
    )
    assert missing.status_code == 404


async def test_a_segment_deleted_with_staff_links_removes_them(client):
    owner, org = await practice(client)
    gold = await create_segment(client, owner, org, name="Gold")
    member = await new_staff(client, owner, org)
    url = org_url(org, f"/staff/{member['id']}/segments")
    await client.put(url, headers=owner.headers, json={"segment_ids": [gold["id"]]})
    deleted = await client.delete(org_url(org, f"/segments/{gold['id']}"), headers=owner.headers)
    assert ok_data(deleted)["outcome"] == "deleted"
    assert ok_data(await client.get(url, headers=owner.headers))["segments"] == []


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
    existing = await create_segment(client, owner, org, name="Existing")
    assert (await client.get(org_url(org, "/segments"), headers=actor.headers)).status_code == 200
    one = await client.get(org_url(org, f"/segments/{existing['id']}"), headers=actor.headers)
    assert one.status_code == 200
    created = await client.post(
        org_url(org, "/segments"), headers=actor.headers, json={"name": "N"}
    )
    patched = await client.patch(
        org_url(org, f"/segments/{existing['id']}"), headers=actor.headers, json={"sort_order": 4}
    )
    off = await client.post(
        org_url(org, f"/segments/{existing['id']}/deactivate"), headers=actor.headers
    )
    deleted = await client.delete(
        org_url(org, f"/segments/{existing['id']}"), headers=actor.headers
    )
    codes = (created.status_code, patched.status_code, off.status_code, deleted.status_code)
    assert codes == ((201, 200, 200, 200) if can_write else (403,) * 4)


@pytest.mark.parametrize(
    ("role", "can_read", "can_write"),
    [
        ("owner", True, True),
        ("admin", True, True),
        ("manager", True, False),
        ("staff", True, False),
        ("viewer", True, False),
    ],
)
async def test_staff_segments_need_member_read_and_member_update(client, role, can_read, can_write):
    owner, org = await practice(client)
    actor = owner if role == "owner" else await staff(org, role)
    gold = await create_segment(client, owner, org, name="Gold")
    member = await new_staff(client, owner, org)
    url = org_url(org, f"/staff/{member['id']}/segments")
    assert (await client.get(url, headers=actor.headers)).status_code == (200 if can_read else 403)
    put = await client.put(url, headers=actor.headers, json={"segment_ids": [gold["id"]]})
    assert put.status_code == (200 if can_write else 403)


async def test_other_practices_segments_are_invisible(client):
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other")
    theirs = await create_segment(client, other_owner, other, name="Theirs")
    url = org_url(org, f"/segments/{theirs['id']}")
    assert (await client.get(url, headers=owner.headers)).status_code == 404
    assert (await client.delete(url, headers=owner.headers)).status_code == 404
    assert ok_data(await client.get(org_url(org, "/segments"), headers=owner.headers)) == []
    member = await new_staff(client, owner, org)
    put = await client.put(
        org_url(org, f"/staff/{member['id']}/segments"),
        headers=owner.headers,
        json={"segment_ids": [theirs["id"]]},
    )
    assert put.status_code == 422


async def test_the_migration_turns_the_values_in_use_into_segments(client):
    """Downgrade one step, put free-text values on clients, upgrade: one segment per value."""
    _, org = await practice(client)
    _, other = await practice(client, "Other")
    for name, org_, value in (
        ("A", org, "Gold"),
        ("B", org, "gold "),
        ("C", org, "Silver"),
        ("D", org, "  "),
        ("E", org, None),
        ("F", other, "Gold"),
    ):
        await make_client(org_, name, segment=value)
    deleted = await make_client(org, "G", segment="Gone")
    await sql("UPDATE clients SET deleted_at = now() WHERE id = :i", i=str(deleted.id))

    def alembic(*command: str) -> None:
        subprocess.run(  # noqa: S603
            [sys.executable, "-m", "alembic", *command], cwd=ROOT, check=True, capture_output=True
        )

    # Re-run the data step of the migration by stepping 062 down and up.
    alembic("downgrade", "061")
    alembic("upgrade", "head")
    rows = await sql(
        "SELECT organization_id::text AS o, name FROM segments ORDER BY organization_id, name"
    )
    mine = sorted(r["name"].lower() for r in rows if r["o"] == org["id"])
    theirs = sorted(r["name"].lower() for r in rows if r["o"] == other["id"])
    assert mine == ["gold", "silver"] and theirs == ["gold"]
