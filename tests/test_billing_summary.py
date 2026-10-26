"""The client's billing summary: hours, value by source, other costs, and cost against budget."""

from datetime import date

from tests.billing_helpers import new_practice
from tests.domain_factories import make_client, make_disbursement, make_job, make_time_entry, staff
from tests.factories import error

DAY = date(2026, 9, 7)


async def summary(client, actor, p, client_id=None):
    response = await client.get(
        p.url(f"/clients/{client_id or p.client.id}/billing-summary"), headers=actor.headers
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def job_row(data: dict, name: str) -> dict:
    return next(row for row in data["jobs"] if row["name"] == name)


async def test_hours_are_shown_even_when_nothing_can_be_billed(client):
    p = await new_practice(client)
    job = await make_job(
        p.org, p.client, "Annual Accounts", billing_basis="fixed", fixed_fee=200_000
    )
    # Non-billable time on a fixed fee job: nothing to bill, but the hours are spent.
    await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=300, is_billable=False)
    data = await summary(client, p.owner, p)
    row = job_row(data, "Annual Accounts")
    assert row["terms"] == "Fixed fee of £2,000.00"
    assert row["breakdown"]["logged_minutes"] == 300
    assert (
        row["breakdown"]["non_billable_minutes"] == 300
        and row["breakdown"]["billable_minutes"] == 0
    )
    assert row["breakdown"]["fixed_fee"] == 200_000 and row["breakdown"]["fixed_fee_billed"] == 0
    assert data["totals"]["logged_minutes"] == 300


async def test_time_is_valued_at_company_or_contract_rates(client):
    p = await new_practice(client)
    job = await make_job(p.org, p.client, "Bookkeeping")
    await make_time_entry(
        p.org,
        p.owner,
        client=p.client,
        job=job,
        minutes=60,
        charge_rate=10_000,
        rate_source="staff",
    )
    await make_time_entry(
        p.org,
        p.owner,
        client=p.client,
        job=job,
        minutes=120,
        charge_rate=8_000,
        rate_source="client",
    )
    await make_time_entry(
        p.org, p.owner, client=p.client, job=job, minutes=30, charge_rate=12_000, rate_source="job"
    )
    await make_disbursement(p.org, p.client, 1_500, job=job)
    row = job_row(await summary(client, p.owner, p), "Bookkeeping")["breakdown"]
    assert row["company_time_value"] == 10_000
    assert row["contract_time_value"] == 16_000 + 6_000
    assert row["ready_time_value"] == 32_000
    assert row["other_costs"] == 1_500
    assert row["billable_total"] == 33_500
    assert row["billable_minutes"] == 210 and row["ready_minutes"] == 210


async def test_the_three_billing_bases_come_to_different_figures(client):
    p = await new_practice(client)
    job = await make_job(p.org, p.client, "Advisory", budget_fee=50_000)
    await make_time_entry(
        p.org, p.owner, client=p.client, job=job, minutes=120, charge_rate=10_000, cost_rate=3_000
    )
    await make_disbursement(p.org, p.client, 2_000, job=job)
    basis = job_row(await summary(client, p.owner, p), "Advisory")["breakdown"]["basis_values"]
    assert basis["contract"] == 20_000 + 2_000  # the time at its rates, and the disbursement
    assert basis["job"] == 50_000 + 2_000  # the fee set for the job, none billed yet
    assert basis["team"] == 6_000 + 2_000  # the cost of the people who did the work


async def test_actual_cost_budgeted_cost_and_agreed_price_are_tracked_separately(client):
    p = await new_practice(client)
    job = await make_job(
        p.org,
        p.client,
        "Audit",
        billing_basis="fixed",
        fixed_fee=300_000,
        budget_cost=90_000,
    )
    await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=600, cost_rate=5_000)
    row = job_row(await summary(client, p.owner, p), "Audit")["breakdown"]
    assert row["actual_cost"] == 50_000
    assert row["budgeted_cost"] == 90_000
    assert row["agreed_price"] == 300_000


async def test_unapproved_time_is_counted_apart_from_what_is_ready(client):
    p = await new_practice(client)
    job = await make_job(p.org, p.client, "Tax return")
    await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=60)
    await make_time_entry(
        p.org,
        p.owner,
        client=p.client,
        job=job,
        minutes=30,
        timesheet_status="submitted",
        entry_date=date(2026, 9, 21),
    )
    row = job_row(await summary(client, p.owner, p), "Tax return")["breakdown"]
    assert row["ready_minutes"] == 60 and row["awaiting_approval_minutes"] == 30
    assert row["logged_minutes"] == 90


async def test_time_without_a_job_is_shown_on_its_own_row(client):
    p = await new_practice(client)
    await make_time_entry(p.org, p.owner, client=p.client, minutes=45)
    data = await summary(client, p.owner, p)
    [row] = data["jobs"]
    assert row["job_id"] is None and row["name"] == "Work without a job"
    assert row["breakdown"]["logged_minutes"] == 45


async def test_billed_time_moves_out_of_what_is_ready(client):
    p = await new_practice(client)
    job = await make_job(p.org, p.client, "Payroll")
    await make_time_entry(
        p.org, p.owner, client=p.client, job=job, minutes=60, billing_status="billed"
    )
    await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=30)
    row = job_row(await summary(client, p.owner, p), "Payroll")["breakdown"]
    assert row["billed_minutes"] == 60 and row["ready_minutes"] == 30
    assert row["billed_time_value"] == 12_000


async def test_cost_needs_rate_read_and_the_route_needs_wip_read(client):
    p = await new_practice(client)
    job = await make_job(p.org, p.client, "Advisory")
    await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=60)
    biller = await staff(p.org, "billing")  # reads WIP, not rates
    data = await summary(client, biller, p)
    breakdown = job_row(data, "Advisory")["breakdown"]
    assert data["show_cost"] is False
    assert breakdown["actual_cost"] is None and breakdown["basis_values"]["team"] is None
    viewer = await staff(p.org, "staff")
    denied = await client.get(
        p.url(f"/clients/{p.client.id}/billing-summary"), headers=viewer.headers
    )
    assert denied.status_code == 403 and error(denied)["code"] == "PERMISSION_DENIED"


async def test_another_practices_client_is_not_found(client):
    p = await new_practice(client)
    other = await new_practice(client, "Other Practice")
    response = await client.get(
        p.url(f"/clients/{other.client.id}/billing-summary"), headers=p.owner.headers
    )
    assert response.status_code == 404
    _ = await make_client(p.org, "Second")


async def test_the_cost_of_each_ready_entry_is_listed_for_billing_on_the_team(client):
    p = await new_practice(client)
    job = await make_job(p.org, p.client, "Advisory")
    first = await make_time_entry(
        p.org, p.owner, client=p.client, job=job, minutes=60, cost_rate=3_000
    )
    await make_time_entry(
        p.org,
        p.owner,
        client=p.client,
        job=job,
        minutes=60,
        is_billable=False,
        entry_date=date(2026, 9, 14),
    )
    data = await summary(client, p.owner, p)
    assert data["time_costs"] == {str(first.id): 3_000}
    biller = await staff(p.org, "billing")
    assert (await summary(client, biller, p))["time_costs"] is None


async def test_the_budgeted_cost_is_set_on_the_job_and_shows_in_the_summary(client):
    p = await new_practice(client)
    job = await make_job(p.org, p.client, "Audit")
    await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=60)
    response = await client.patch(
        p.url(f"/jobs/{job.id}"), headers=p.owner.headers, json={"budget_cost": 45_000}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["budget_cost"] == 45_000
    row = job_row(await summary(client, p.owner, p), "Audit")["breakdown"]
    assert row["budgeted_cost"] == 45_000
    cleared = await client.patch(
        p.url(f"/jobs/{job.id}"), headers=p.owner.headers, json={"budget_cost": None}
    )
    assert cleared.json()["data"]["budget_cost"] is None
