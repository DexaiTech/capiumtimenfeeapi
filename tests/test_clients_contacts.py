"""Client contacts (one primary) and engagements (client services)."""

import asyncio
import uuid

import pytest

from tests.clients_helpers import create_client, org_url, scalar
from tests.domain_factories import make_client, practice, service_id, staff
from tests.factories import audit_entries, error


def contacts_url(org: dict, client_id: str, contact_id: str | None = None) -> str:
    base = org_url(org, f"/clients/{client_id}/contacts")
    return f"{base}/{contact_id}" if contact_id else base


async def primaries(client_id: str) -> list[str]:
    from tests.clients_helpers import sql

    rows = await sql(
        "SELECT name FROM client_contacts WHERE client_id = :c AND is_primary ORDER BY name",
        c=client_id,
    )
    return [r["name"] for r in rows]


# --- Contacts ---------------------------------------------------------------------------


async def test_first_contact_becomes_primary_and_a_new_primary_replaces_it(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org)
    url = contacts_url(org, acme["id"])
    first = await client.post(url, headers=owner.headers, json={"name": "Jane", "email": "j@x.io"})
    assert first.status_code == 201, first.text
    assert first.json()["data"]["is_primary"] is True
    second = await client.post(url, headers=owner.headers, json={"name": "Bob", "is_billing": True})
    assert second.json()["data"]["is_primary"] is False
    assert second.json()["data"]["is_billing"] is True
    third = await client.post(url, headers=owner.headers, json={"name": "Cleo", "is_primary": True})
    assert third.json()["data"]["is_primary"] is True
    assert await primaries(acme["id"]) == ["Cleo"]

    listed = (await client.get(url, headers=owner.headers)).json()["data"]
    assert [c["name"] for c in listed] == ["Cleo", "Bob", "Jane"]
    detail = (
        await client.get(org_url(org, f"/clients/{acme['id']}"), headers=owner.headers)
    ).json()
    assert detail["data"]["contact_name"] == "Cleo"


async def test_patch_moves_the_primary_flag(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org, contacts=[{"name": "Jane"}, {"name": "Bob"}])
    bob = next(c for c in acme["contacts"] if c["name"] == "Bob")
    response = await client.patch(
        contacts_url(org, acme["id"], bob["id"]),
        headers=owner.headers,
        json={"is_primary": True, "phone": "01234 567890", "email": "bob@acme.example"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["is_primary"] is True
    assert await primaries(acme["id"]) == ["Bob"]
    # Un-flagging leaves the client without a primary contact.
    response = await client.patch(
        contacts_url(org, acme["id"], bob["id"]), headers=owner.headers, json={"is_primary": False}
    )
    assert await primaries(acme["id"]) == []
    for body in ({"name": None}, {"is_primary": None}, {"email": "nope"}, {"extra": 1}):
        response = await client.patch(
            contacts_url(org, acme["id"], bob["id"]), headers=owner.headers, json=body
        )
        assert response.status_code == 422, body
    audit = (await audit_entries(org["id"], "client_contact.updated"))[0]["metadata"]
    assert audit["changes"]["phone"] == [None, "01234 567890"]


async def test_delete_contact(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org, contacts=[{"name": "Jane"}])
    jane = acme["contacts"][0]
    url = contacts_url(org, acme["id"], jane["id"])
    assert (await client.delete(url, headers=owner.headers)).status_code == 200
    assert (await client.delete(url, headers=owner.headers)).status_code == 404
    detail = (
        await client.get(org_url(org, f"/clients/{acme['id']}"), headers=owner.headers)
    ).json()
    assert detail["data"]["contacts"] == [] and detail["data"]["contact_name"] is None
    assert (await audit_entries(org["id"], "client_contact.deleted"))[0]["metadata"][
        "name"
    ] == "Jane"


async def test_concurrent_primary_contacts_end_with_one_primary(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org)
    responses = await asyncio.gather(
        *(
            client.post(
                contacts_url(org, acme["id"]),
                headers=owner.headers,
                json={"name": f"Person {i}", "is_primary": True},
            )
            for i in range(5)
        )
    )
    assert [r.status_code for r in responses] == [201] * 5
    assert len(await primaries(acme["id"])) == 1


async def test_concurrent_first_contacts_end_with_one_primary(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org)
    responses = await asyncio.gather(
        *(
            client.post(
                contacts_url(org, acme["id"]), headers=owner.headers, json={"name": f"P{i}"}
            )
            for i in range(4)
        )
    )
    assert [r.status_code for r in responses] == [201] * 4
    assert len(await primaries(acme["id"])) == 1


@pytest.mark.parametrize(
    "body",
    [{}, {"name": ""}, {"name": "x" * 121}, {"name": "X", "email": "bad"}, {"name": "X", "a": 1}],
)
async def test_contact_validation(client, body):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org)
    response = await client.post(contacts_url(org, acme["id"]), headers=owner.headers, json=body)
    assert response.status_code == 422


async def test_contact_permissions_and_tenancy(client):
    owner, org = await practice(client)
    _, other = await practice(client, "Other")
    acme = await create_client(client, owner, org, contacts=[{"name": "Jane"}])
    second = await create_client(client, owner, org, "Second")
    jane = acme["contacts"][0]
    foreign = await make_client(other)
    member = await staff(org)
    assert (
        await client.get(contacts_url(org, acme["id"]), headers=member.headers)
    ).status_code == 200
    assert (
        await client.post(contacts_url(org, acme["id"]), headers=member.headers, json={"name": "X"})
    ).status_code == 403
    assert (
        await client.patch(
            contacts_url(org, acme["id"], jane["id"]), headers=member.headers, json={"name": "X"}
        )
    ).status_code == 403
    assert (
        await client.delete(contacts_url(org, acme["id"], jane["id"]), headers=member.headers)
    ).status_code == 403
    billing = await staff(org, "billing")
    assert (
        await client.post(
            contacts_url(org, acme["id"]), headers=billing.headers, json={"name": "B"}
        )
    ).status_code == 201
    # Another practice's client, and a contact addressed through the wrong client.
    assert (
        await client.get(contacts_url(org, str(foreign.id)), headers=owner.headers)
    ).status_code == 404
    assert (
        await client.post(
            contacts_url(org, str(foreign.id)), headers=owner.headers, json={"name": "X"}
        )
    ).status_code == 404
    assert (
        await client.patch(
            contacts_url(org, second["id"], jane["id"]), headers=owner.headers, json={"name": "X"}
        )
    ).status_code == 404
    assert (
        await client.delete(contacts_url(org, acme["id"], str(uuid.uuid4())), headers=owner.headers)
    ).status_code == 404


# --- Engagements ------------------------------------------------------------------------


def services_url(org: dict, client_id: str, engagement_id: str | None = None) -> str:
    base = org_url(org, f"/clients/{client_id}/services")
    return f"{base}/{engagement_id}" if engagement_id else base


async def test_engagement_crud(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org)
    vat = await service_id(org, "VAT")
    books = await service_id(org, "BOOKS")
    response = await client.post(
        services_url(org, acme["id"]),
        headers=owner.headers,
        json={"service_id": str(vat), "frequency": "quarterly", "start_date": "2026-04-01"},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["service_code"] == "VAT" and data["service_name"] == "VAT return"
    assert data["fee"] is None and data["effective_fee"] == 15_000
    assert data["frequency"] == "quarterly" and data["is_active"] is True
    hourly = await client.post(
        services_url(org, acme["id"]), headers=owner.headers, json={"service_id": str(books)}
    )
    assert hourly.json()["data"]["effective_fee"] is None
    assert hourly.json()["data"]["billing_basis"] == "hourly"

    url = services_url(org, acme["id"], data["id"])
    response = await client.patch(
        url, headers=owner.headers, json={"fee": 18_000, "end_date": "2027-03-31"}
    )
    assert response.status_code == 200
    assert response.json()["data"]["effective_fee"] == 18_000
    response = await client.patch(url, headers=owner.headers, json={"end_date": "2026-01-01"})
    assert response.status_code == 422  # before the start date
    response = await client.patch(url, headers=owner.headers, json={"is_active": False})
    assert response.json()["data"]["is_active"] is False

    listed = (await client.get(services_url(org, acme["id"]), headers=owner.headers)).json()["data"]
    assert [e["service_code"] for e in listed] == ["BOOKS", "VAT"]  # active first
    assert (await client.delete(url, headers=owner.headers)).status_code == 200
    assert (await client.delete(url, headers=owner.headers)).status_code == 404
    actions = [
        a["action"]
        for a in await audit_entries(org["id"])
        if a["action"].startswith("client_service")
    ]
    assert actions == [
        "client_service.created",
        "client_service.created",
        "client_service.updated",
        "client_service.updated",
        "client_service.deleted",
    ]


async def test_engagement_rules(client):
    owner, org = await practice(client)
    _, other = await practice(client, "Other")
    acme = await create_client(client, owner, org)
    vat = await service_id(org, "VAT")
    url = services_url(org, acme["id"])
    assert (
        await client.post(url, headers=owner.headers, json={"service_id": str(vat)})
    ).status_code == 201
    response = await client.post(url, headers=owner.headers, json={"service_id": str(vat)})
    assert response.status_code == 409
    assert error(response)["code"] == "CLIENT_SERVICE_EXISTS"

    accs = await service_id(org, "ACCS")
    await client.post(org_url(org, f"/services/{accs}/deactivate"), headers=owner.headers)
    response = await client.post(url, headers=owner.headers, json={"service_id": str(accs)})
    assert response.status_code == 422
    foreign = await service_id(other, "CT")
    response = await client.post(url, headers=owner.headers, json={"service_id": str(foreign)})
    assert response.status_code == 422
    for body in (
        {"service_id": str(vat), "fee": -1},
        {"service_id": str(vat), "frequency": "weekly"},
        {"service_id": str(vat), "start_date": "2026-05-01", "end_date": "2026-04-01"},
        {},
    ):
        assert (await client.post(url, headers=owner.headers, json=body)).status_code == 422


async def test_engagement_permissions_and_tenancy(client):
    owner, org = await practice(client)
    _, other = await practice(client, "Other")
    acme = await create_client(client, owner, org)
    foreign = await make_client(other)
    vat = await service_id(org, "VAT")
    member = await staff(org)
    url = services_url(org, acme["id"])
    assert (await client.get(url, headers=member.headers)).status_code == 200
    assert (
        await client.post(url, headers=member.headers, json={"service_id": str(vat)})
    ).status_code == 403
    created = (await client.post(url, headers=owner.headers, json={"service_id": str(vat)})).json()[
        "data"
    ]
    assert (
        await client.patch(
            services_url(org, acme["id"], created["id"]), headers=member.headers, json={"fee": 1}
        )
    ).status_code == 403
    assert (
        await client.get(services_url(org, str(foreign.id)), headers=owner.headers)
    ).status_code == 404
    second = await create_client(client, owner, org, "Second")
    assert (
        await client.patch(
            services_url(org, second["id"], created["id"]), headers=owner.headers, json={"fee": 1}
        )
    ).status_code == 404
    assert (
        await scalar("SELECT count(*) FROM client_services WHERE client_id = :c", c=acme["id"]) == 1
    )
