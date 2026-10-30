"""Leads: adding and editing them with their costs, the status lists and totals, archiving,
the one-step conversion into a client, permissions, tenant isolation and the audit trail."""

import asyncio
import uuid

import pytest
from httpx import AsyncClient

from tests.audit_helpers import history
from tests.clients_helpers import create_client, org_url, scalar
from tests.domain_factories import practice, staff
from tests.factories import audit_entries, error
from tests.time_helpers import ok_data

PREFIX = "/api/v1/organizations/{organization_id}"


async def create_lead(client: AsyncClient, actor, org: dict, status: int = 201, **body):
    body = {"name": "Bloggs Bakery Ltd"} | body
    response = await client.post(org_url(org, "/leads"), headers=actor.headers, json=body)
    assert response.status_code == status, response.text
    return response.json()["data"] if status < 300 else error(response)


async def act(client: AsyncClient, actor, org: dict, lead: dict, action: str, **kwargs):
    return await client.post(
        org_url(org, f"/leads/{lead['id']}/{action}"), headers=actor.headers, **kwargs
    )


async def listed(client: AsyncClient, actor, org: dict, query: str = "") -> list[dict]:
    return ok_data(await client.get(org_url(org, f"/leads{query}"), headers=actor.headers))


async def test_a_lead_is_added_with_its_costs_and_read_back(client):
    owner, org = await practice(client)
    lead = await create_lead(
        client,
        owner,
        org,
        name="  Bloggs Bakery Ltd ",
        client_type="sole_trader",
        contact_name="Joe Bloggs",
        email="joe@bloggs.example",
        phone=" ",
        source="Referral",
        acquisition_cost=12_500,
        onboarding_cost=4_999,
    )
    assert lead["name"] == "Bloggs Bakery Ltd" and lead["client_type"] == "sole_trader"
    assert (lead["contact_name"], lead["email"], lead["phone"]) == (
        "Joe Bloggs",
        "joe@bloggs.example",
        None,
    )
    assert (lead["acquisition_cost"], lead["onboarding_cost"], lead["total_cost"]) == (
        12_500,
        4_999,
        17_499,
    )
    assert lead["status"] == "open" and lead["converted_client"] is None
    assert lead["created_by"] == str(owner.id)
    one = ok_data(await client.get(org_url(org, f"/leads/{lead['id']}"), headers=owner.headers))
    assert one == lead
    plain = await create_lead(client, owner, org, name="Plain")
    assert (plain["client_type"], plain["acquisition_cost"], plain["onboarding_cost"]) == (
        "limited_company",
        0,
        0,
    )


async def test_fields_are_validated(client):
    owner, org = await practice(client)
    for body in (
        {"name": ""},
        {"name": "x" * 201},
        {"name": "A", "acquisition_cost": -1},
        {"name": "A", "onboarding_cost": -5},
        {"name": "A", "acquisition_cost": 10**12 + 1},
        {"name": "A", "email": "not-an-email"},
        {"name": "A", "client_type": "company"},
        {"name": "A", "source": "s" * 101},
        {"name": "A", "contact_name": "c" * 121},
        {"name": "A", "nope": 1},
        {},
    ):
        response = await client.post(org_url(org, "/leads"), headers=owner.headers, json=body)
        assert response.status_code == 422, body
    lead = await create_lead(client, owner, org)
    url = org_url(org, f"/leads/{lead['id']}")
    for body in (
        {"name": None},
        {"client_type": None},
        {"acquisition_cost": None},
        {"onboarding_cost": -1},
        {"email": "bad"},
    ):
        assert (await client.patch(url, headers=owner.headers, json=body)).status_code == 422, body


async def test_editing_changes_fields_clears_text_and_is_audited(client):
    owner, org = await practice(client)
    lead = await create_lead(client, owner, org, source="Website", acquisition_cost=1_000)
    url = org_url(org, f"/leads/{lead['id']}")
    changed = ok_data(
        await client.patch(
            url, headers=owner.headers, json={"acquisition_cost": 2_500, "source": None}
        )
    )
    assert changed["acquisition_cost"] == 2_500 and changed["source"] is None
    assert changed["total_cost"] == 2_500
    # Nothing changed: no second entry.
    assert (
        await client.patch(url, headers=owner.headers, json={"acquisition_cost": 2_500})
    ).status_code == 200
    [entry] = await audit_entries(org["id"], "lead.updated")
    assert entry["target_type"] == "lead" and entry["target_id"] == lead["id"]
    assert entry["metadata"]["changes"] == {
        "acquisition_cost": [1_000, 2_500],
        "source": ["Website", None],
    }
    missing = await client.patch(
        org_url(org, f"/leads/{uuid.uuid4()}"), headers=owner.headers, json={"name": "X"}
    )
    assert missing.status_code == 404


async def test_the_lists_filter_by_status_and_search_newest_first(client):
    owner, org = await practice(client)
    first = await create_lead(client, owner, org, name="Alpha Ltd", contact_name="Ann Smith")
    second = await create_lead(client, owner, org, name="Beta Ltd", email="ops@beta.example")
    third = await create_lead(client, owner, org, name="Gamma Ltd")
    gone = await create_lead(client, owner, org, name="Delta Ltd")
    assert (await act(client, owner, org, gone, "archive")).status_code == 200
    won = await create_lead(client, owner, org, name="Epsilon Ltd")
    assert (await act(client, owner, org, won, "convert", json={})).status_code == 200
    open_ids = [lead["id"] for lead in await listed(client, owner, org)]
    assert open_ids == [third["id"], second["id"], first["id"]]
    assert [lead["id"] for lead in await listed(client, owner, org, "?status=converted")] == [
        won["id"]
    ]
    assert [lead["id"] for lead in await listed(client, owner, org, "?status=archived")] == [
        gone["id"]
    ]
    for query, expected in (("smith", first), ("BETA.EXAMPLE", second), ("gamma", third)):
        found = await listed(client, owner, org, f"?q={query}")
        assert [lead["id"] for lead in found] == [expected["id"]], query
    bad = await client.get(org_url(org, "/leads?status=lost"), headers=owner.headers)
    assert bad.status_code == 422


async def test_the_list_is_cursor_paginated(client):
    owner, org = await practice(client)
    made = [(await create_lead(client, owner, org, name=f"Lead {i}"))["id"] for i in range(5)]
    seen: list[str] = []
    url = org_url(org, "/leads?limit=2")
    while True:
        response = await client.get(url, headers=owner.headers)
        body = response.json()
        seen += [lead["id"] for lead in ok_data(response)]
        if not body["meta"]["has_more"]:
            break
        url = org_url(org, f"/leads?limit=2&cursor={body['meta']['next_cursor']}")
    assert seen == list(reversed(made))


async def test_the_summary_totals_each_status(client):
    owner, org = await practice(client)
    empty = ok_data(await client.get(org_url(org, "/leads/summary"), headers=owner.headers))
    zero = {"count": 0, "acquisition_cost": 0, "onboarding_cost": 0, "total_cost": 0}
    assert empty == {"open": zero, "converted": zero, "archived": zero}
    await create_lead(client, owner, org, acquisition_cost=100, onboarding_cost=50)
    await create_lead(client, owner, org, acquisition_cost=200)
    won = await create_lead(client, owner, org, acquisition_cost=1_000, onboarding_cost=300)
    await act(client, owner, org, won, "convert")
    gone = await create_lead(client, owner, org, acquisition_cost=7)
    await act(client, owner, org, gone, "archive")
    summary = ok_data(await client.get(org_url(org, "/leads/summary"), headers=owner.headers))
    assert summary == {
        "open": {"count": 2, "acquisition_cost": 300, "onboarding_cost": 50, "total_cost": 350},
        "converted": {
            "count": 1,
            "acquisition_cost": 1_000,
            "onboarding_cost": 300,
            "total_cost": 1_300,
        },
        "archived": {"count": 1, "acquisition_cost": 7, "onboarding_cost": 0, "total_cost": 7},
    }


async def test_archiving_and_unarchiving(client):
    owner, org = await practice(client)
    lead = await create_lead(client, owner, org)
    archived = ok_data(await act(client, owner, org, lead, "archive"))
    assert archived["status"] == "archived" and archived["archived_at"]
    # Twice changes nothing.
    assert ok_data(await act(client, owner, org, lead, "archive"))["archived_at"] == (
        archived["archived_at"]
    )
    assert await listed(client, owner, org) == []
    back = ok_data(await act(client, owner, org, lead, "unarchive"))
    assert back["status"] == "open" and back["archived_at"] is None
    assert [e["target_id"] for e in await audit_entries(org["id"], "lead.archived")] == [
        lead["id"]
    ]
    assert len(await audit_entries(org["id"], "lead.unarchived")) == 1


async def test_converting_creates_the_client_and_its_primary_contact(client):
    owner, org = await practice(client)
    lead = await create_lead(
        client,
        owner,
        org,
        name="Bloggs Bakery Ltd",
        client_type="partnership",
        contact_name="Joe Bloggs",
        email="joe@bloggs.example",
        phone="01234 567890",
        notes="Met at the chamber of commerce",
        acquisition_cost=12_500,
    )
    response = await act(client, owner, org, lead, "convert")
    converted = ok_data(response)
    assert response.json()["message"] == "Lead converted to a client"
    assert converted["status"] == "converted" and converted["converted_at"]
    ref = converted["converted_client"]
    assert ref["id"] == converted["converted_client_id"] and ref["name"] == "Bloggs Bakery Ltd"
    detail = ok_data(
        await client.get(org_url(org, f"/clients/{ref['id']}"), headers=owner.headers)
    )
    assert detail["code"] == ref["code"]
    assert (detail["client_type"], detail["status"], detail["email"], detail["phone"]) == (
        "partnership",
        "active",
        "joe@bloggs.example",
        "01234 567890",
    )
    assert detail["notes"] == "Met at the chamber of commerce"
    [contact] = detail["contacts"]
    assert (contact["name"], contact["email"], contact["is_primary"]) == (
        "Joe Bloggs",
        "joe@bloggs.example",
        True,
    )
    # The costs stay editable after conversion.
    later = await client.patch(
        org_url(org, f"/leads/{lead['id']}"), headers=owner.headers, json={"onboarding_cost": 800}
    )
    assert ok_data(later)["onboarding_cost"] == 800 and ok_data(later)["status"] == "converted"
    [made] = await audit_entries(org["id"], "client.created")
    assert made["target_id"] == ref["id"]
    [entry] = await audit_entries(org["id"], "lead.converted")
    assert entry["target_type"] == "lead" and entry["target_id"] == lead["id"]
    assert entry["metadata"] == {
        "name": "Bloggs Bakery Ltd",
        "client_id": ref["id"],
        "client_code": ref["code"],
    }


async def test_a_lead_without_a_contact_converts_with_the_code_given(client):
    owner, org = await practice(client)
    lead = await create_lead(client, owner, org, name="Solo")
    converted = ok_data(await act(client, owner, org, lead, "convert", json={"code": " sol1 "}))
    assert converted["converted_client"]["code"] == "SOL1"
    contacts = await scalar(
        "SELECT count(*) FROM client_contacts WHERE client_id = :i",
        i=converted["converted_client_id"],
    )
    assert contacts == 0


async def test_a_lead_converts_once(client):
    owner, org = await practice(client)
    lead = await create_lead(client, owner, org)
    first = ok_data(await act(client, owner, org, lead, "convert"))
    again = await act(client, owner, org, lead, "convert")
    assert again.status_code == 409
    assert error(again)["code"] == "LEAD_ALREADY_CONVERTED"
    assert error(again)["details"] == {"client_id": first["converted_client_id"]}
    clients = await scalar(
        "SELECT count(*) FROM clients WHERE organization_id = :o", o=org["id"]
    )
    assert clients == 1


async def test_two_clicks_at_once_make_one_client(client):
    owner, org = await practice(client)
    lead = await create_lead(client, owner, org)
    responses = await asyncio.gather(
        act(client, owner, org, lead, "convert"), act(client, owner, org, lead, "convert")
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    clients = await scalar(
        "SELECT count(*) FROM clients WHERE organization_id = :o", o=org["id"]
    )
    assert clients == 1


async def test_an_archived_lead_must_be_unarchived_to_convert(client):
    owner, org = await practice(client)
    lead = await create_lead(client, owner, org)
    await act(client, owner, org, lead, "archive")
    refused = await act(client, owner, org, lead, "convert")
    assert refused.status_code == 409 and error(refused)["code"] == "LEAD_ARCHIVED"
    await act(client, owner, org, lead, "unarchive")
    assert ok_data(await act(client, owner, org, lead, "convert"))["status"] == "converted"
    # A converted lead can be archived: archived wins.
    hidden = ok_data(await act(client, owner, org, lead, "archive"))
    assert hidden["status"] == "archived" and hidden["converted_client"] is not None


async def test_a_taken_code_refuses_the_conversion_and_leaves_the_lead_open(client):
    owner, org = await practice(client)
    await create_client(client, owner, org, "Existing", code="ACME")
    lead = await create_lead(client, owner, org)
    taken = await act(client, owner, org, lead, "convert", json={"code": "acme"})
    assert taken.status_code == 409 and error(taken)["code"] == "CLIENT_CODE_TAKEN"
    row = ok_data(await client.get(org_url(org, f"/leads/{lead['id']}"), headers=owner.headers))
    assert row["status"] == "open" and row["converted_client_id"] is None
    assert await audit_entries(org["id"], "lead.converted") == []


async def test_a_deleted_client_leaves_the_lead_converted_without_a_link(client):
    owner, org = await practice(client)
    lead = await create_lead(client, owner, org)
    converted = ok_data(await act(client, owner, org, lead, "convert"))
    deleted = await client.delete(
        org_url(org, f"/clients/{converted['converted_client_id']}"), headers=owner.headers
    )
    assert deleted.status_code == 200, deleted.text
    row = ok_data(await client.get(org_url(org, f"/leads/{lead['id']}"), headers=owner.headers))
    assert row["status"] == "converted" and row["converted_client"] is None


async def test_lead_history_is_readable_with_client_read(client):
    owner, org = await practice(client)
    lead = await create_lead(client, owner, org, name="Hist Ltd")
    reader = await staff(org, "staff")
    response = await history(client, reader, org, "lead", lead["id"])
    assert response.status_code == 200, response.text
    assert [e["action"] for e in response.json()["data"]] == ["lead.created"]
    assert response.json()["data"][0]["label"] == "Lead added"


@pytest.mark.parametrize(
    ("role", "can_create", "can_edit", "can_archive"),
    [
        ("owner", True, True, True),
        ("admin", True, True, True),
        ("partner", True, True, True),
        ("manager", True, True, False),
        ("billing", False, True, False),
        ("staff", False, False, False),
        ("viewer", False, False, False),
    ],
)
async def test_permissions_follow_the_clients(client, role, can_create, can_edit, can_archive):
    owner, org = await practice(client)
    actor = owner if role == "owner" else await staff(org, role)
    existing = await create_lead(client, owner, org, name="Existing")
    other = await create_lead(client, owner, org, name="Other")
    assert (await client.get(org_url(org, "/leads"), headers=actor.headers)).status_code == 200
    summary = await client.get(org_url(org, "/leads/summary"), headers=actor.headers)
    assert summary.status_code == 200
    one = await client.get(org_url(org, f"/leads/{existing['id']}"), headers=actor.headers)
    assert one.status_code == 200
    created = await client.post(org_url(org, "/leads"), headers=actor.headers, json={"name": "N"})
    converted = await act(client, actor, org, other, "convert")
    assert (created.status_code, converted.status_code) == (
        (201, 200) if can_create else (403, 403)
    )
    patched = await client.patch(
        org_url(org, f"/leads/{existing['id']}"), headers=actor.headers, json={"notes": "x"}
    )
    assert patched.status_code == (200 if can_edit else 403)
    archived = await act(client, actor, org, existing, "archive")
    unarchived = await act(client, actor, org, existing, "unarchive")
    assert (archived.status_code, unarchived.status_code) == (
        (200, 200) if can_archive else (403, 403)
    )


async def test_other_practices_leads_are_invisible(client):
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other")
    theirs = await create_lead(client, other_owner, other, name="Theirs", acquisition_cost=99)
    url = org_url(org, f"/leads/{theirs['id']}")
    assert (await client.get(url, headers=owner.headers)).status_code == 404
    assert (
        await client.patch(url, headers=owner.headers, json={"name": "Mine"})
    ).status_code == 404
    for action in ("archive", "unarchive", "convert"):
        assert (await act(client, owner, org, theirs, action)).status_code == 404, action
    assert await listed(client, owner, org) == []
    summary = ok_data(await client.get(org_url(org, "/leads/summary"), headers=owner.headers))
    assert summary["open"]["count"] == 0
    outsider = await client.get(org_url(other, "/leads"), headers=owner.headers)
    assert outsider.status_code == 404


async def test_routes_are_documented(client):
    schema = (await client.get("/openapi.json")).json()
    for method, path in (
        ("get", "/leads"),
        ("post", "/leads"),
        ("get", "/leads/summary"),
        ("get", "/leads/{lead_id}"),
        ("patch", "/leads/{lead_id}"),
        ("post", "/leads/{lead_id}/archive"),
        ("post", "/leads/{lead_id}/unarchive"),
        ("post", "/leads/{lead_id}/convert"),
    ):
        operation = schema["paths"][PREFIX + path][method]
        assert operation["tags"] == ["Leads"], (method, path)
        assert operation["summary"] and len(operation["description"]) > 20, (method, path)
        assert {"401", "403", "404", "422"} <= set(operation["responses"]), (method, path)
    convert = schema["paths"][PREFIX + "/leads/{lead_id}/convert"]["post"]
    assert "409" in convert["responses"]
