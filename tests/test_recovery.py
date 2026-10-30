"""The recovery analysis (APT52): invoice value against the task rate, the staff charge-out rates
and cost plus a markup, on the report, the client's Invoices & Recovery and the invoicing screen.
The ticket's worked examples are checked as they are written in it."""

import csv
import io
from datetime import date, timedelta

from tests.billing_helpers import draft, issued, new_practice, set_org
from tests.domain_factories import make_job, make_staff_rate, make_time_entry, practice, staff
from tests.factories import error
from tests.report_helpers import download
from tests.reports_helpers import fetch, report

MONDAY = date(2026, 9, 7)
NAME = "recovery-analysis"


async def twenty_hours(p, job, *, user=None, charge_rate=10_000, cost_rate=3_500) -> list:
    """20 hours on the job over four days, priced at the task rate (£100 an hour by default)."""
    entries = []
    for day in range(4):
        entries.append(
            await make_time_entry(
                p.org,
                user or p.owner,
                client=p.client,
                job=job,
                entry_date=MONDAY + timedelta(days=day),
                minutes=300,
                charge_rate=charge_rate,
                cost_rate=cost_rate,
            )
        )
    return entries


def row_named(data: dict, name: str) -> dict:
    return next(row for row in data["rows"] if row["name"] == name)


async def client_recovery(http, actor, p, client_id=None) -> dict:
    response = await http.get(
        p.url(f"/clients/{client_id or p.client.id}/recovery"), headers=actor.headers
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


# --- the ticket's examples ------------------------------------------------------------------


async def test_a_fixed_fee_against_the_task_rate_and_the_staff_rate(client):
    """£1,500 fixed for annual accounts; the practice usually charges £100 an hour for them; the
    staff member's charge-out rate is £80 (cost £35); the job took 20 hours."""
    p = await new_practice(client)
    await make_staff_rate(p.org, p.owner, 8_000, 3_500)
    job = await make_job(
        p.org, p.client, "Annual accounts", billing_basis="fixed", fixed_fee=150_000
    )
    entries = await twenty_hours(p, job)

    # Before it is invoiced the fixed fee is what the work will bill.
    row = row_named(await report(client, p.owner, p.org, NAME), "Annual accounts")
    assert row["minutes"] == 1_200
    assert (row["invoiced_value"], row["to_bill_value"], row["invoice_value"]) == (
        0,
        150_000,
        150_000,
    )
    assert row["task_value"] == 200_000 and row["task_shortfall"] == 50_000
    assert row["staff_value"] == 160_000 and row["staff_shortfall"] == 10_000
    assert row["task_recovery_bp"] == 7_500 and row["staff_recovery_bp"] == 9_375
    assert row["billing_basis"] == "fixed" and row["fixed_fee"] == 150_000

    # Invoiced: the same figures, now from the invoice.
    invoice = await draft(client, p.owner, p.org, p.client.id, time=entries)
    in_draft = row_named(await report(client, p.owner, p.org, NAME), "Annual accounts")
    assert (in_draft["invoiced_value"], in_draft["to_bill_value"]) == (0, 150_000)
    await issued(client, p.owner, p.org, invoice["id"])
    done = row_named(await report(client, p.owner, p.org, NAME), "Annual accounts")
    assert (done["invoiced_value"], done["to_bill_value"], done["invoice_value"]) == (
        150_000,
        0,
        150_000,
    )
    assert done["task_shortfall"] == 50_000 and done["staff_shortfall"] == 10_000
    # Cost £35 an hour: £700, £1,050 at the default 50% markup.
    assert done["cost_value"] == 70_000 and done["cost_plus_value"] == 105_000
    assert done["cost_plus_shortfall"] == 105_000 - 150_000
    assert done["margin_value"] == 80_000


async def test_two_people_are_valued_at_their_own_charge_out_rates(client):
    """A semi-senior at £50 an hour and a partner at £150, ten hours each: £500 + £1,500."""
    p = await new_practice(client)
    senior = await staff(p.org, "staff", full_name="Sam Semi-Senior")
    partner = await staff(p.org, "partner", full_name="Pat Partner")
    await make_staff_rate(p.org, senior, 5_000, 2_000)
    await make_staff_rate(p.org, partner, 15_000, 6_000)
    job = await make_job(p.org, p.client, "Accounts")
    for person in (senior, partner):
        for day in range(2):
            await make_time_entry(
                p.org,
                person,
                client=p.client,
                job=job,
                entry_date=MONDAY + timedelta(days=day),
                minutes=300,
                charge_rate=10_000,
            )
    data = await report(client, p.owner, p.org, NAME)
    assert row_named(data, "Accounts")["staff_value"] == 200_000
    by_person = await report(client, p.owner, p.org, NAME, group_by="person")
    assert row_named(by_person, "Sam Semi-Senior")["staff_value"] == 50_000
    assert row_named(by_person, "Pat Partner")["staff_value"] == 150_000
    assert by_person["totals"]["staff_value"] == 200_000
    assert by_person["totals"]["minutes"] == 1_200


async def test_cost_plus_adds_the_markup_to_the_cost(client):
    """A person who costs £50 an hour, billed at cost plus 50%: £75 an hour."""
    p = await new_practice(client)
    job = await make_job(p.org, p.client, "Payroll")
    await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=60, cost_rate=5_000)
    row = row_named(await report(client, p.owner, p.org, NAME), "Payroll")
    assert row["cost_value"] == 5_000 and row["cost_plus_value"] == 7_500
    data = await report(client, p.owner, p.org, NAME, markup_bp=2_000)
    assert row_named(data, "Payroll")["cost_plus_value"] == 6_000
    assert data["filters"]["markup_bp"] == 2_000

    response = await client.patch(
        p.url(""), headers=p.owner.headers, json={"cost_plus_markup_bp": 10_000}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["cost_plus_markup_bp"] == 10_000
    row = row_named(await report(client, p.owner, p.org, NAME), "Payroll")
    assert row["cost_plus_value"] == 10_000
    too_much = await client.patch(
        p.url(""), headers=p.owner.headers, json={"cost_plus_markup_bp": 100_001}
    )
    assert too_much.status_code == 422


# --- the invoice value ---------------------------------------------------------------------


async def test_hourly_work_bills_its_value_and_written_off_work_nothing(client):
    p = await new_practice(client)
    job = await make_job(p.org, p.client, "Bookkeeping")
    await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=60)
    await make_time_entry(
        p.org,
        p.owner,
        client=p.client,
        job=job,
        minutes=30,
        entry_date=MONDAY + timedelta(days=1),
        billing_status="written_off",
    )
    row = row_named(await report(client, p.owner, p.org, NAME), "Bookkeeping")
    assert row["minutes"] == 90
    assert row["task_value"] == 12_000 + 6_000
    assert row["invoice_value"] == 12_000  # the written-off half hour bills nothing
    assert row["task_shortfall"] == 6_000


async def test_a_filter_takes_its_share_of_the_fixed_fee(client):
    p = await new_practice(client)
    job = await make_job(p.org, p.client, "Audit", billing_basis="fixed", fixed_fee=150_000)
    await twenty_hours(p, job)
    whole = row_named(await report(client, p.owner, p.org, NAME), "Audit")
    assert whole["invoice_value"] == 150_000
    first_day = row_named(
        await report(
            client, p.owner, p.org, NAME, date_from=MONDAY.isoformat(), date_to=MONDAY.isoformat()
        ),
        "Audit",
    )
    assert first_day["minutes"] == 300 and first_day["invoice_value"] == 37_500


async def test_rows_by_client_and_the_totals(client):
    p = await new_practice(client)
    job = await make_job(p.org, p.client, "Accounts", billing_basis="fixed", fixed_fee=150_000)
    await twenty_hours(p, job)
    data = await report(client, p.owner, p.org, NAME, group_by="client")
    [row] = data["rows"]
    assert row["name"] == "Widgets Ltd" and row["client_id"] == str(p.client.id)
    assert data["totals"]["invoice_value"] == 150_000
    assert data["totals"]["task_value"] == 200_000


# --- cost visibility, permissions and downloads ------------------------------------------------


async def test_cost_is_hidden_without_rate_read(client):
    p = await new_practice(client)
    job = await make_job(p.org, p.client, "Advisory")
    await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=60)
    biller = await staff(p.org, "billing")  # reports on money, does not read rates
    data = await report(client, biller, p.org, NAME)
    row = row_named(data, "Advisory")
    for field in (
        "cost_value",
        "cost_plus_value",
        "cost_plus_shortfall",
        "cost_plus_recovery_bp",
        "margin_value",
        "margin_bp",
    ):
        assert row[field] is None and data["totals"][field] is None, field
    assert row["task_value"] == 12_000 and row["staff_value"] is not None
    table = list(csv.reader(io.StringIO((await download(client, biller, p.org, NAME, "csv")).text)))
    cost = table[0].index("Cost")
    assert table[1][cost] == "" and table[-1][cost] == ""


async def test_the_report_needs_report_financial(client):
    _, org = await practice(client)
    viewer = await staff(org, "staff")
    response = await fetch(client, viewer, org, NAME)
    assert response.status_code == 403 and error(response)["code"] == "PERMISSION_DENIED"


async def test_the_csv_names_the_bases(client):
    p = await new_practice(client)
    await make_staff_rate(p.org, p.owner, 8_000, 3_500)
    job = await make_job(
        p.org, p.client, "Annual accounts", billing_basis="fixed", fixed_fee=150_000
    )
    await twenty_hours(p, job)
    response = await download(client, p.owner, p.org, NAME, "csv")
    assert response.status_code == 200, response.text
    table = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    header, first, last = table[0], table[1], table[-1]
    assert header[:3] == ["Job", "Job number", "Client"]
    values = dict(zip(header, first, strict=True))
    assert values["Hours"] == "20.00"
    assert values["Invoice value"] == "1500.00"
    assert values["At task rate"] == "2000.00"
    assert values["Left on table (task rate)"] == "500.00"
    assert values["At staff rates"] == "1600.00"
    assert values["Left on table (staff rates)"] == "100.00"
    assert last[0] == "Total"


# --- the client's Invoices & Recovery ---------------------------------------------------------


async def test_the_client_view_lists_each_job_and_the_totals(client):
    p = await new_practice(client)
    await make_staff_rate(p.org, p.owner, 8_000, 3_500)
    job = await make_job(
        p.org, p.client, "Annual accounts", billing_basis="fixed", fixed_fee=150_000
    )
    await twenty_hours(p, job)
    await make_time_entry(p.org, p.owner, client=p.client, minutes=60, entry_date=MONDAY)
    await make_job(p.org, p.client, "No time yet")
    data = await client_recovery(client, p.owner, p)
    names = [row["name"] for row in data["jobs"]]
    assert names == ["Annual accounts", "Work without a job"]
    accounts = data["jobs"][0]
    assert accounts["terms"] == "Fixed fee of £1,500.00"
    assert accounts["invoice_value"] == 150_000 and accounts["task_shortfall"] == 50_000
    assert accounts["staff_shortfall"] == 10_000
    loose = data["jobs"][1]
    assert loose["job_id"] is None and loose["invoice_value"] == 12_000
    assert data["totals"]["minutes"] == 1_260
    assert data["markup_bp"] == 5_000 and data["show_cost"] is True
    # The report says the same for the client.
    same = await report(client, p.owner, p.org, NAME, client_id=p.client.id)
    assert same["totals"]["invoice_value"] == data["totals"]["invoice_value"]
    assert same["totals"]["staff_value"] == data["totals"]["staff_value"]


async def test_the_client_view_needs_wip_read_and_hides_cost_without_rates(client):
    p = await new_practice(client)
    job = await make_job(p.org, p.client, "Advisory")
    await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=60)
    viewer = await staff(p.org, "viewer")  # reads WIP, not rates
    data = await client_recovery(client, viewer, p)
    assert data["show_cost"] is False and data["totals"]["cost_value"] is None
    worker = await staff(p.org, "staff")
    denied = await client.get(p.url(f"/clients/{p.client.id}/recovery"), headers=worker.headers)
    assert denied.status_code == 403
    other = await new_practice(client, "Other Practice")
    missing = await client.get(
        p.url(f"/clients/{other.client.id}/recovery"), headers=p.owner.headers
    )
    assert missing.status_code == 404


# --- the invoicing screen ----------------------------------------------------------------------


async def test_the_billing_summary_values_the_work_ready_to_bill_on_every_basis(client):
    p = await new_practice(client)
    await make_staff_rate(p.org, p.owner, 8_000, 3_500)
    job = await make_job(
        p.org, p.client, "Annual accounts", billing_basis="fixed", fixed_fee=150_000
    )
    await twenty_hours(p, job)
    response = await client.get(
        p.url(f"/clients/{p.client.id}/billing-summary"), headers=p.owner.headers
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    [row] = data["jobs"]
    recovery = row["breakdown"]["recovery"]
    assert recovery == {
        "minutes": 1_200,
        "task_value": 200_000,
        "staff_value": 160_000,
        "cost_value": 70_000,
        "cost_plus_value": 105_000,
    }
    assert data["totals"]["recovery"] == recovery
    assert data["markup_bp"] == 5_000
    # What the invoice would bill on the contract: the fixed fee, £500 short of the task rate.
    assert row["breakdown"]["basis_values"]["contract"] == 150_000

    await set_org(p.org, cost_plus_markup_bp=0)
    biller = await staff(p.org, "billing")
    hidden = await client.get(
        p.url(f"/clients/{p.client.id}/billing-summary"), headers=biller.headers
    )
    totals = hidden.json()["data"]["totals"]["recovery"]
    assert totals["cost_value"] is None and totals["cost_plus_value"] is None
    assert totals["staff_value"] == 160_000
