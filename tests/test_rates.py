"""Staff rate history, current staff rates and client-specific rates."""

import uuid
from datetime import date, timedelta

import pytest

from app.db.session import get_sessionmaker
from app.services.rates import current_rates, rate_in_force
from tests.clients_helpers import create_client, org_url
from tests.domain_factories import make_client, make_staff_rate, practice, staff
from tests.factories import audit_entries, error


def rates_url(org: dict, user) -> str:
    return org_url(org, f"/members/{user.id}/rates")


# --- Staff rates ------------------------------------------------------------------------


async def test_add_and_list_rate_history_newest_first(client):
    owner, org = await practice(client)
    ada = await staff(org, full_name="Ada Junior")
    for start, charge, cost in (("2025-04-01", 9000, 3000), ("2026-04-01", 10000, 3500)):
        response = await client.post(
            rates_url(org, ada),
            headers=owner.headers,
            json={"effective_from": start, "charge_rate": charge, "cost_rate": cost},
        )
        assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert data["charge_rate"] == 10000 and data["cost_rate"] == 3500
    assert data["created_by"] == str(owner.id)

    listed = (await client.get(rates_url(org, ada), headers=owner.headers)).json()["data"]
    assert [r["effective_from"] for r in listed] == ["2026-04-01", "2025-04-01"]
    audit = (await audit_entries(org["id"], "staff_rate.created"))[-1]["metadata"]
    assert audit["member_name"] == "Ada Junior" and audit["charge_rate"] == 10000


async def test_cost_rate_defaults_to_zero(client):
    owner, org = await practice(client)
    ada = await staff(org)
    response = await client.post(
        rates_url(org, ada),
        headers=owner.headers,
        json={"effective_from": "2026-01-01", "charge_rate": 5000},
    )
    assert response.json()["data"]["cost_rate"] == 0


async def test_duplicate_effective_from_is_409(client):
    owner, org = await practice(client)
    ada = await staff(org)
    body = {"effective_from": "2026-01-01", "charge_rate": 5000}
    assert (
        await client.post(rates_url(org, ada), headers=owner.headers, json=body)
    ).status_code == 201
    response = await client.post(rates_url(org, ada), headers=owner.headers, json=body)
    assert response.status_code == 409
    assert error(response)["code"] == "STAFF_RATE_EXISTS"


@pytest.mark.parametrize(
    "body",
    [
        {"effective_from": "2026-01-01", "charge_rate": -1},
        {"effective_from": "2026-01-01", "charge_rate": 100, "cost_rate": -5},
        {"effective_from": "not a date", "charge_rate": 100},
        {"charge_rate": 100},
        {"effective_from": "2026-01-01", "charge_rate": 10.5},
        {"effective_from": "2026-01-01", "charge_rate": 100, "user_id": str(uuid.uuid4())},
    ],
)
async def test_staff_rate_validation(client, body):
    owner, org = await practice(client)
    ada = await staff(org)
    response = await client.post(rates_url(org, ada), headers=owner.headers, json=body)
    assert response.status_code == 422, response.text


async def test_staff_rate_permissions(client):
    _owner, org = await practice(client)
    ada = await staff(org)
    body = {"effective_from": "2026-01-01", "charge_rate": 5000}
    for role in ("staff", "billing", "viewer"):
        actor = await staff(org, role)
        assert (await client.get(rates_url(org, ada), headers=actor.headers)).status_code == 403
        assert (
            await client.get(org_url(org, "/staff-rates/current"), headers=actor.headers)
        ).status_code == 403
    # Managers read rates but do not set them.
    manager = await staff(org, "manager")
    assert (await client.get(rates_url(org, ada), headers=manager.headers)).status_code == 200
    assert (
        await client.post(rates_url(org, ada), headers=manager.headers, json=body)
    ).status_code == 403
    rate = await make_staff_rate(org, ada, 4000)
    assert (
        await client.delete(org_url(org, f"/staff-rates/{rate.id}"), headers=manager.headers)
    ).status_code == 403
    partner = await staff(org, "partner")
    assert (
        await client.post(rates_url(org, ada), headers=partner.headers, json=body)
    ).status_code == 201


async def test_rate_manage_without_rate_read_never_sees_cost_rates(client):
    """domain.md: nobody without rate.read sees cost_rate. A custom role may hold
    rate.manage alone; the rate it adds comes back with a null cost."""
    owner, org = await practice(client)
    response = await client.post(
        org_url(org, "/roles"),
        headers=owner.headers,
        json={"name": "Rate setter", "permissions": ["rate.manage"]},
    )
    assert response.status_code == 201, response.text
    setter = await staff(org, "Rate setter")
    ada = await staff(org)
    response = await client.post(
        rates_url(org, ada),
        headers=setter.headers,
        json={"effective_from": "2026-01-01", "charge_rate": 5000, "cost_rate": 2000},
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["charge_rate"] == 5000
    assert response.json()["data"]["cost_rate"] is None
    # Stored all the same, and visible to someone with rate.read.
    listed = (await client.get(rates_url(org, ada), headers=owner.headers)).json()["data"]
    assert listed[0]["cost_rate"] == 2000
    assert (await client.get(rates_url(org, ada), headers=setter.headers)).status_code == 403


async def test_rates_of_someone_outside_the_practice_are_404(client):
    owner, org = await practice(client)
    _, other = await practice(client, "Other")
    outsider = await staff(other)
    body = {"effective_from": "2026-01-01", "charge_rate": 5000}
    assert (await client.get(rates_url(org, outsider), headers=owner.headers)).status_code == 404
    assert (
        await client.post(rates_url(org, outsider), headers=owner.headers, json=body)
    ).status_code == 404
    foreign_rate = await make_staff_rate(other, outsider, 1000)
    response = await client.delete(
        org_url(org, f"/staff-rates/{foreign_rate.id}"), headers=owner.headers
    )
    assert response.status_code == 404


async def test_removed_member_cannot_get_new_rates(client):
    owner, org = await practice(client)
    gone = await staff(org, status="removed")
    response = await client.post(
        rates_url(org, gone),
        headers=owner.headers,
        json={"effective_from": "2026-01-01", "charge_rate": 5000},
    )
    assert response.status_code == 409
    assert error(response)["code"] == "MEMBER_REMOVED"
    # Their history stays readable.
    assert (await client.get(rates_url(org, gone), headers=owner.headers)).status_code == 200


async def test_delete_staff_rate(client):
    owner, org = await practice(client)
    ada = await staff(org, full_name="Ada Deleted-Rate")
    rate = await make_staff_rate(org, ada, 4000, 1000)
    response = await client.delete(org_url(org, f"/staff-rates/{rate.id}"), headers=owner.headers)
    assert response.status_code == 200
    assert (await client.get(rates_url(org, ada), headers=owner.headers)).json()["data"] == []
    assert (
        await client.delete(org_url(org, f"/staff-rates/{rate.id}"), headers=owner.headers)
    ).status_code == 404
    meta = (await audit_entries(org["id"], "staff_rate.deleted"))[0]["metadata"]
    assert meta["charge_rate"] == 4000
    # The audit row names the person, not just their id.
    assert meta["member_name"] == "Ada Deleted-Rate"


async def test_current_staff_rates(client):
    owner, org = await practice(
        client,
    )
    ada = await staff(org, full_name="Ada Able")
    bob = await staff(org, full_name="Bob Baker")
    await staff(org, full_name="Sam Suspended", status="suspended")
    today = date.today()
    await make_staff_rate(org, ada, 9000, 3000, effective_from=today - timedelta(days=400))
    await make_staff_rate(org, ada, 10000, 3500, effective_from=today - timedelta(days=30))
    await make_staff_rate(org, ada, 12000, 4000, effective_from=today + timedelta(days=60))
    response = await client.get(org_url(org, "/staff-rates/current"), headers=owner.headers)
    assert response.status_code == 200
    rows = {r["full_name"]: r for r in response.json()["data"]}
    assert "Sam Suspended" not in rows
    assert set(rows) == {"Ada Able", "Bob Baker", owner.full_name}
    assert rows["Ada Able"]["charge_rate"] == 10000
    assert rows["Ada Able"]["cost_rate"] == 3500
    assert rows["Bob Baker"]["charge_rate"] is None and rows["Bob Baker"]["rate_id"] is None
    names = [r["full_name"] for r in response.json()["data"]]
    assert names == sorted(names, key=str.lower)

    future = (today + timedelta(days=61)).isoformat()
    response = await client.get(
        org_url(org, f"/staff-rates/current?on_date={future}"), headers=owner.headers
    )
    rows = {r["full_name"]: r for r in response.json()["data"]}
    assert rows["Ada Able"]["charge_rate"] == 12000
    assert rows["Ada Able"]["on_date"] == future
    assert str(bob.id) == rows["Bob Baker"]["user_id"]


async def test_rate_in_force_and_current_rates_helpers(client):
    _owner, org = await practice(client)
    _, other = await practice(client, "Other")
    ada = await staff(org)
    await make_staff_rate(org, ada, 9000, 3000, effective_from=date(2025, 4, 1))
    await make_staff_rate(org, ada, 10000, 3500, effective_from=date(2026, 4, 1))
    org_id = uuid.UUID(org["id"])
    async with get_sessionmaker()() as session:
        assert await rate_in_force(session, org_id, ada.id, date(2025, 3, 31)) is None
        assert await rate_in_force(session, org_id, ada.id, date(2025, 4, 1)) == (9000, 3000)
        assert await rate_in_force(session, org_id, ada.id, date(2026, 3, 31)) == (9000, 3000)
        assert await rate_in_force(session, org_id, ada.id, date(2026, 4, 1)) == (10000, 3500)
        # Another practice's rows are never seen.
        assert (
            await rate_in_force(session, uuid.UUID(other["id"]), ada.id, date(2027, 1, 1)) is None
        )
        assert await current_rates(session, org_id, date(2026, 5, 1)) == {ada.id: (10000, 3500)}


# --- Client rates -----------------------------------------------------------------------


async def test_client_rates_crud(client):
    owner, org = await practice(client)
    ada = await staff(org, full_name="Ada Able")
    acme = await create_client(client, owner, org)
    url = org_url(org, f"/clients/{acme['id']}/rates")
    everyone = await client.post(url, headers=owner.headers, json={"hourly_rate": 15000})
    assert everyone.status_code == 201, everyone.text
    assert everyone.json()["data"]["user_id"] is None
    personal = await client.post(
        url, headers=owner.headers, json={"user_id": str(ada.id), "hourly_rate": 18000}
    )
    assert personal.status_code == 201
    assert personal.json()["data"]["user_name"] == "Ada Able"

    listed = (await client.get(url, headers=owner.headers)).json()["data"]
    assert [r["user_id"] for r in listed] == [None, str(ada.id)]

    rate_id = personal.json()["data"]["id"]
    response = await client.patch(
        f"{url}/{rate_id}", headers=owner.headers, json={"hourly_rate": 19000}
    )
    assert response.status_code == 200
    assert response.json()["data"]["hourly_rate"] == 19000
    audit = (await audit_entries(org["id"], "client_rate.updated"))[0]["metadata"]
    assert audit["changes"]["hourly_rate"] == [18000, 19000]

    assert (await client.delete(f"{url}/{rate_id}", headers=owner.headers)).status_code == 200
    assert len((await client.get(url, headers=owner.headers)).json()["data"]) == 1
    # Every client-rate audit row names the client (domain.md, "Audit").
    for action in ("client_rate.created", "client_rate.updated", "client_rate.deleted"):
        meta = (await audit_entries(org["id"], action))[-1]["metadata"]
        assert meta["client_code"] == acme["code"], action
        assert meta["client_name"] == acme["name"], action
    deleted = (await audit_entries(org["id"], "client_rate.deleted"))[0]["metadata"]
    assert deleted["member_name"] == "Ada Able" and deleted["hourly_rate"] == 19000


async def test_duplicate_client_rates_are_409(client):
    owner, org = await practice(client)
    ada = await staff(org)
    acme = await create_client(client, owner, org)
    url = org_url(org, f"/clients/{acme['id']}/rates")
    for body in ({"hourly_rate": 100}, {"user_id": str(ada.id), "hourly_rate": 100}):
        assert (await client.post(url, headers=owner.headers, json=body)).status_code == 201
        response = await client.post(url, headers=owner.headers, json=body)
        assert response.status_code == 409
        assert error(response)["code"] == "CLIENT_RATE_EXISTS"


async def test_client_rate_person_must_be_a_member(client):
    owner, org = await practice(client)
    _, other = await practice(client, "Other")
    outsider = await staff(other)
    acme = await create_client(client, owner, org)
    url = org_url(org, f"/clients/{acme['id']}/rates")
    for body in (
        {"user_id": str(outsider.id), "hourly_rate": 100},
        {"user_id": str(uuid.uuid4()), "hourly_rate": 100},
        {"hourly_rate": -1},
        {"hourly_rate": None},
    ):
        response = await client.post(url, headers=owner.headers, json=body)
        assert response.status_code == 422, body


async def test_client_rate_permissions_and_tenancy(client):
    owner, org = await practice(client)
    _, other = await practice(client, "Other")
    acme = await create_client(client, owner, org)
    foreign = await make_client(other)
    url = org_url(org, f"/clients/{acme['id']}/rates")
    member = await staff(org)
    manager = await staff(org, "manager")
    assert (await client.get(url, headers=member.headers)).status_code == 403
    assert (await client.get(url, headers=manager.headers)).status_code == 200
    assert (
        await client.post(url, headers=manager.headers, json={"hourly_rate": 1})
    ).status_code == 403
    foreign_url = org_url(org, f"/clients/{foreign.id}/rates")
    assert (await client.get(foreign_url, headers=owner.headers)).status_code == 404
    assert (
        await client.post(foreign_url, headers=owner.headers, json={"hourly_rate": 1})
    ).status_code == 404
    rate = (await client.post(url, headers=owner.headers, json={"hourly_rate": 1})).json()["data"]
    # The rate exists, but not under another client.
    other_client = await create_client(client, owner, org, "Second Ltd")
    response = await client.patch(
        org_url(org, f"/clients/{other_client['id']}/rates/{rate['id']}"),
        headers=owner.headers,
        json={"hourly_rate": 2},
    )
    assert response.status_code == 404


async def test_client_detail_shows_rates_only_with_rate_read(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org)
    await client.post(
        org_url(org, f"/clients/{acme['id']}/rates"), headers=owner.headers, json={"hourly_rate": 1}
    )
    detail = (
        await client.get(org_url(org, f"/clients/{acme['id']}"), headers=owner.headers)
    ).json()
    assert [r["hourly_rate"] for r in detail["data"]["rates"]] == [1]
    member = await staff(org)
    detail = (
        await client.get(org_url(org, f"/clients/{acme['id']}"), headers=member.headers)
    ).json()
    assert detail["data"]["rates"] is None
