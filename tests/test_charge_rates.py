"""The charge-out rate ladder, cost rates and explicit re-pricing (``/rates/revalue``).

The per-level tests of the dated rate rules are in ``test_rates_v2.py``."""

import uuid
from datetime import date

from httpx import AsyncClient

from app.db.models import Organization
from app.db.session import get_sessionmaker
from app.services import charge_rates
from app.services.charge_rates import RateQuery
from tests.domain_factories import make_client, make_job, make_staff_rate, practice, staff
from tests.factories import audit_entries
from tests.time_helpers import (
    entry_row,
    err,
    log_time,
    make_client_rate,
    ok_data,
    org_url,
    set_entry,
    set_practice,
)


async def _org(org: dict) -> Organization:
    async with get_sessionmaker()() as session:
        found = await session.get(Organization, uuid.UUID(org["id"]))
        assert found is not None
        return found


async def test_hierarchy_each_step(client: AsyncClient) -> None:
    _, org = await practice(client)
    await set_practice(org, default_charge_rate=5_000, time_increment_minutes=1)
    person = await staff(org)
    other = await staff(org)
    acme = await make_client(org)
    job = await make_job(org, acme, hourly_rate=30_000)
    plain_job = await make_job(org, acme)
    beta = await make_client(org)

    # The practice default
    assert (await log_time(client, person, org, client_id=beta.id))["charge_rate"] == 5_000
    # The person's staff rate in force
    await make_staff_rate(org, person, 8_000, 2_500)
    assert (await log_time(client, person, org, client_id=beta.id))["charge_rate"] == 8_000
    # The job's rate beats the staff rate
    entry = await log_time(client, person, org, job_id=job.id, minutes=45)
    assert entry["charge_rate"] == 30_000 and entry["charge_value"] == 22_500
    # A zero job rate is still a rate ("exists"), not a fall-through.
    free = await make_job(org, beta, hourly_rate=0)
    assert (await log_time(client, person, org, job_id=free.id))["charge_rate"] == 0
    # The client's rate for everyone beats the job's rate
    await make_client_rate(org, acme.id, 11_000)
    assert (await log_time(client, person, org, job_id=job.id))["charge_rate"] == 11_000
    assert (await log_time(client, other, org, client_id=acme.id))["charge_rate"] == 11_000
    assert (await log_time(client, person, org, job_id=plain_job.id))["charge_rate"] == 11_000
    # The client's rate for the person beats the one for everyone
    await make_client_rate(org, acme.id, 13_000, person)
    assert (await log_time(client, person, org, job_id=plain_job.id))["charge_rate"] == 13_000
    assert (await log_time(client, other, org, client_id=acme.id))["charge_rate"] == 11_000
    # Cost is the staff rate (0 without one), whatever priced the charge.
    row = await entry_row(entry["id"])
    assert row.cost_rate == 2_500 and row.cost_value == 1_875
    other_row = await entry_row((await log_time(client, other, org, job_id=job.id))["id"])
    assert other_row.cost_rate == 0 and other_row.cost_value == 0


async def test_staff_rate_history_by_entry_date(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    await make_staff_rate(org, person, 6_000, 1_000, date(2026, 1, 1))
    await make_staff_rate(org, person, 7_000, 1_500, date(2026, 9, 8))
    await make_staff_rate(org, person, 9_000, 2_000, date(2027, 1, 1))
    organization = await _org(org)
    async with get_sessionmaker()() as session:
        cases = {
            # Before the first rate: the practice default charge, and the first cost rate
            # (APT51: a person who has a cost never costs nothing).
            date(2025, 12, 31): (organization.default_charge_rate, 1_000),
            date(2026, 1, 1): (6_000, 1_000),
            date(2026, 9, 7): (6_000, 1_000),
            date(2026, 9, 8): (7_000, 1_500),
            date(2026, 12, 31): (7_000, 1_500),
            date(2027, 6, 1): (9_000, 2_000),
        }
        for day, expected in cases.items():
            assert (
                await charge_rates.resolve(session, organization, person.id, None, None, day)
                == expected
            )
        # Batched: same answers, in order, with the source of each.
        found = await charge_rates.resolve_many(
            session, organization, [RateQuery(person.id, None, None, d) for d in cases]
        )
        assert [(r.charge_rate, r.cost_rate) for r in found] == list(cases.values())
        assert [r.source for r in found] == [
            "default",
            "staff",
            "staff",
            "staff",
            "staff",
            "staff",
        ]


async def test_resolve_ignores_other_practices(client: AsyncClient) -> None:
    _, org = await practice(client)
    _, other = await practice(client, "Other & Co")
    person = await staff(org)
    theirs = await make_client(other)
    their_job = await make_job(other, theirs, hourly_rate=99_000)
    await make_client_rate(other, theirs.id, 88_000)
    organization = await _org(org)
    async with get_sessionmaker()() as session:
        assert await charge_rates.resolve(
            session, organization, person.id, theirs.id, their_job.id, date(2026, 9, 7)
        ) == (0, 0)


async def test_rate_changes_do_not_rewrite_entries(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    await make_staff_rate(org, person, 6_000)
    entry = await log_time(client, person, org, client_id=acme.id)
    await make_client_rate(org, acme.id, 20_000)
    assert (await entry_row(entry["id"])).charge_rate == 6_000
    # ... until the entry is next changed (re-taken while unbilled).
    response = await client.patch(
        org_url(org, f"/time-entries/{entry['id']}"),
        headers=person.headers,
        json={"description": "x"},
    )
    assert ok_data(response)["charge_rate"] == 20_000


async def test_revalue(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    other = await staff(org)
    acme = await make_client(org)
    beta = await make_client(org)
    await make_staff_rate(org, person, 6_000, 1_000)
    await make_staff_rate(org, other, 6_000, 1_000)
    e1 = await log_time(client, person, org, client_id=acme.id, minutes=60, entry_date="2026-09-07")
    e2 = await log_time(client, person, org, client_id=beta.id, minutes=30, entry_date="2026-09-08")
    e3 = await log_time(client, other, org, client_id=acme.id, minutes=60, entry_date="2026-09-08")
    billed = await log_time(
        client, person, org, client_id=acme.id, minutes=60, entry_date="2026-09-09"
    )
    outside = await log_time(
        client, person, org, client_id=acme.id, minutes=60, entry_date="2026-10-01"
    )
    await set_entry(billed["id"], billing_status="billed")
    await make_staff_rate(org, person, 9_000, 2_000, date(2026, 9, 1))
    await make_client_rate(org, acme.id, 12_000)
    url = org_url(org, "/rates/revalue")

    # Only the person's entries.
    result = ok_data(
        await client.post(
            url,
            headers=owner.headers,
            json={"date_from": "2026-09-01", "date_to": "2026-09-30", "user_id": str(person.id)},
        )
    )
    assert result["entries_examined"] == 2  # e1, e2 (billed and out-of-range skipped)
    assert result["entries_changed"] == 2
    assert result["charge_value_before"] == 6_000 + 3_000
    assert result["charge_value_after"] == 12_000 + 4_500
    assert result["value_delta"] == 7_500
    assert result["cost_value_delta"] == (2_000 - 1_000) + (1_000 - 500)
    assert (await entry_row(e1["id"])).charge_rate == 12_000
    assert (await entry_row(e2["id"])).charge_rate == 9_000
    assert (await entry_row(billed["id"])).charge_rate == 6_000
    assert (await entry_row(outside["id"])).charge_rate == 6_000
    assert (await entry_row(e3["id"])).charge_rate == 6_000

    # By client; running again changes nothing.
    result = ok_data(
        await client.post(
            url,
            headers=owner.headers,
            json={"date_from": "2026-09-01", "date_to": "2026-09-30", "client_id": str(acme.id)},
        )
    )
    assert result["entries_examined"] == 2 and result["entries_changed"] == 1  # e3
    assert result["value_delta"] == 6_000
    again = ok_data(
        await client.post(
            url, headers=owner.headers, json={"date_from": "2026-09-01", "date_to": "2026-09-30"}
        )
    )
    assert again["entries_changed"] == 0 and again["value_delta"] == 0
    log = await audit_entries(org["id"], "time_entries.revalued")
    assert len(log) == 3 and log[0]["metadata"]["entries_changed"] == 2


async def test_revalue_permissions_and_validation(client: AsyncClient) -> None:
    _, org = await practice(client)
    url = org_url(org, "/rates/revalue")
    body = {"date_from": "2026-09-01", "date_to": "2026-09-30"}
    for role in ("manager", "staff", "billing", "viewer"):
        person = await staff(org, role)
        assert (await client.post(url, headers=person.headers, json=body)).status_code == 403, role
    admin = await staff(org, "admin")
    assert (await client.post(url, headers=admin.headers, json=body)).status_code == 200
    bad = {"date_from": "2026-09-30", "date_to": "2026-09-01"}
    assert (
        err(await client.post(url, headers=admin.headers, json=bad), 422)["details"][0]["field"]
        == "date_to"
    )
    assert (
        await client.post(url, headers=admin.headers, json={"date_from": "2026-09-01"})
    ).status_code == 422
    other_owner, _ = await practice(client, "Other & Co")
    assert (await client.post(url, headers=other_owner.headers, json=body)).status_code == 404
