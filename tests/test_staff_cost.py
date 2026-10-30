"""APT51: the cost of time is the person's cost rate, whatever the job and its billing basis.

A cost rate added "from today" also costs time dated before it, and time recorded while the
person had no cost rate at all takes it once one is added. Billed time and the charge-out side
are never touched."""

from datetime import date

from httpx import AsyncClient

from tests.billing_helpers import new_practice
from tests.domain_factories import make_job, staff
from tests.factories import audit_entries
from tests.team_helpers import bill, grade_id, new_rate, set_grade
from tests.time_helpers import entry_row, log_time, ok_data, org_url

# Ada logged an hour on 05/10/2026; her £60/h cost rate was entered later, from 30/10/2026.
LOGGED = date(2026, 10, 5)
RATE_FROM = date(2026, 10, 30)


async def summary_row(client: AsyncClient, p, job_name: str) -> dict:
    response = await client.get(
        p.url(f"/clients/{p.client.id}/billing-summary"), headers=p.owner.headers
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    return next(row for row in data["jobs"] if row["name"] == job_name)["breakdown"]


async def test_the_actual_cost_of_an_hourly_job_is_the_staff_cost_rate(client: AsyncClient):
    p = await new_practice(client)
    ada = await staff(p.org)
    await new_rate(
        client, p.owner, p.org, kind="cost", user_id=ada.id, rate=6_000, effective_from=RATE_FROM
    )
    job = await make_job(p.org, p.client, "Annual Accounts", hourly_rate=10_000)

    entry = await log_time(client, ada, p.org, job_id=job.id, minutes=60, entry_date=LOGGED)

    row = await entry_row(entry["id"])
    assert (row.charge_rate, row.charge_value) == (10_000, 10_000)
    assert (row.cost_rate, row.cost_value) == (6_000, 6_000)
    breakdown = await summary_row(client, p, "Annual Accounts")
    assert breakdown["actual_cost"] == 6_000
    assert breakdown["contract_time_value"] == 10_000  # the charge side is unchanged


async def test_the_actual_cost_of_a_fixed_fee_job_is_the_staff_cost_rate(client: AsyncClient):
    p = await new_practice(client)
    ada = await staff(p.org)
    await new_rate(
        client, p.owner, p.org, kind="cost", user_id=ada.id, rate=6_000, effective_from=RATE_FROM
    )
    job = await make_job(
        p.org, p.client, "Year End", billing_basis="fixed", fixed_fee=150_000
    )

    entry = await log_time(client, ada, p.org, job_id=job.id, minutes=90, entry_date=LOGGED)

    assert (await entry_row(entry["id"])).cost_value == 9_000
    assert (await summary_row(client, p, "Year End"))["actual_cost"] == 9_000


async def test_time_recorded_without_a_cost_takes_the_cost_rate_once_it_is_added(
    client: AsyncClient,
):
    p = await new_practice(client)
    ada = await staff(p.org)
    job = await make_job(p.org, p.client, "Annual Accounts", hourly_rate=10_000)
    open_entry = await log_time(client, ada, p.org, job_id=job.id, minutes=60, entry_date=LOGGED)
    billed_entry = await log_time(
        client, ada, p.org, job_id=job.id, minutes=30, entry_date=LOGGED
    )
    assert (await entry_row(open_entry["id"])).cost_value == 0
    await bill(billed_entry["id"])

    rule = await new_rate(
        client, p.owner, p.org, kind="cost", user_id=ada.id, rate=6_000, effective_from=RATE_FROM
    )

    filled = await entry_row(open_entry["id"])
    assert (filled.cost_rate, filled.cost_value) == (6_000, 6_000)
    assert (filled.charge_rate, filled.charge_value) == (10_000, 10_000)  # untouched
    billed = await entry_row(billed_entry["id"])
    assert (billed.cost_rate, billed.cost_value) == (0, 0)  # billed time never changes
    [log] = await audit_entries(p.org["id"], "time_entries.costed")
    assert log["target_type"] == "rate_rule" and str(log["target_id"]) == rule["id"]
    assert log["metadata"]["entries"] == 1 and log["metadata"]["cost_value"] == 6_000


async def test_a_grade_cost_rate_fills_in_the_cost_of_its_staff(client: AsyncClient):
    p = await new_practice(client)
    ada = await staff(p.org)
    await set_grade(p.org, ada, "SNR")
    job = await make_job(p.org, p.client, "Annual Accounts", hourly_rate=10_000)
    entry = await log_time(client, ada, p.org, job_id=job.id, minutes=60, entry_date=LOGGED)
    assert (await entry_row(entry["id"])).cost_value == 0

    snr = await grade_id(p.org, "SNR")
    ok_data(
        await client.patch(
            org_url(p.org, f"/grades/{snr}"),
            headers=p.owner.headers,
            json={"default_cost_rate": 4_000},
        )
    )

    assert (await entry_row(entry["id"])).cost_value == 4_000
    assert (await summary_row(client, p, "Annual Accounts"))["actual_cost"] == 4_000
