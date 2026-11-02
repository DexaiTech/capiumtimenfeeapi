"""The recovery analysis (APT52): invoice value against the recovery rate (the practice's order of
the client job-specific, task and staff rates), the staff charge-out rates and cost plus a
mark-up, on the report, the client's Invoices & Recovery and the invoicing screen; and the Cost
to Business report. The ticket's worked examples are checked as they are written in it, with the
client's answers: a fixed fee counts once it is invoiced, credit notes allocated to an invoice
reduce it, mark-ups are set per practice, client and job."""

import csv
import io
import uuid
from datetime import date, timedelta

from tests.billing_helpers import Practice, draft, issued, new_practice, pay, set_org
from tests.domain_factories import make_job, make_staff_rate, make_time_entry, practice, staff
from tests.factories import error
from tests.report_helpers import download
from tests.reports_helpers import fetch, report
from tests.team_helpers import new_rate

MONDAY = date(2026, 9, 7)
NAME = "recovery-analysis"
COST = "cost-to-business"


async def task_rate(http, p: Practice, rate: int = 10_000, person=None) -> uuid.UUID:
    """A service whose rate for the person (the owner by default) is ``rate`` an hour."""
    response = await http.post(
        p.url("/services"),
        headers=p.owner.headers,
        json={"code": f"S{uuid.uuid4().hex[:6].upper()}", "name": f"Task {uuid.uuid4().hex[:6]}"},
    )
    assert response.status_code == 201, response.text
    service_id = response.json()["data"]["id"]
    await new_rate(
        http,
        p.owner,
        p.org,
        kind="charge",
        service_id=service_id,
        user_id=(person or p.owner).id,
        rate=rate,
        effective_from="2026-01-01",
    )
    return uuid.UUID(service_id)


async def twenty_hours(p, job, *, user=None, cost_rate=3_500) -> list:
    """20 hours on the job over four days."""
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
                charge_rate=10_000,
                cost_rate=cost_rate,
            )
        )
    return entries


async def annual_accounts(http, p: Practice):
    """The ticket's example: £1,500 fixed for annual accounts; the practice usually charges £100
    an hour for them (the task rate); the staff member's charge-out rate is £80, they cost £35;
    the job took 20 hours."""
    await make_staff_rate(p.org, p.owner, 8_000, 3_500)
    service_id = await task_rate(http, p)
    job = await make_job(
        p.org,
        p.client,
        "Annual accounts",
        billing_basis="fixed",
        fixed_fee=150_000,
        service_id=service_id,
    )
    return job, await twenty_hours(p, job)


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
    p = await new_practice(client)
    _, entries = await annual_accounts(client, p)

    # Not invoiced yet: the fixed fee is only WIP; there is no invoice value to compare.
    row = row_named(await report(client, p.owner, p.org, NAME), "Annual accounts")
    assert (row["minutes"], row["invoice_value"], row["recovery_value"]) == (0, 0, 0)
    assert row["wip_minutes"] == 1_200 and row["wip_value"] == 200_000

    # A draft invoice exists: it is the invoice value.
    invoice = await draft(client, p.owner, p.org, p.client.id, time=entries)
    in_draft = row_named(await report(client, p.owner, p.org, NAME), "Annual accounts")
    assert (in_draft["draft_value"], in_draft["invoiced_value"], in_draft["invoice_value"]) == (
        150_000,
        0,
        150_000,
    )
    assert in_draft["minutes"] == 1_200 and in_draft["wip_minutes"] == 0

    await issued(client, p.owner, p.org, invoice["id"])
    done = row_named(await report(client, p.owner, p.org, NAME), "Annual accounts")
    assert (done["invoiced_value"], done["draft_value"], done["invoice_value"]) == (
        150_000,
        0,
        150_000,
    )
    # Potential recovery (task rate) 20 x £100 = £2,000: £500 left on the table.
    assert done["recovery_value"] == 200_000 and done["recovery_shortfall"] == 50_000
    assert done["task_minutes"] == 1_200 and done["client_job_minutes"] == 0
    assert done["recovery_bp"] == 7_500
    # Potential recovery (staff rate) 20 x £80 = £1,600: £100 left.
    assert done["staff_value"] == 160_000 and done["staff_shortfall"] == 10_000
    assert done["staff_recovery_bp"] == 9_375
    assert done["billing_basis"] == "fixed" and done["fixed_fee"] == 150_000
    # Cost £35 an hour: £700, £1,050 at the default 50% mark-up.
    assert done["cost_value"] == 70_000 and done["cost_plus_value"] == 105_000
    assert done["cost_plus_shortfall"] == 105_000 - 150_000
    assert done["margin_value"] == 80_000 and done["markup_bp"] == 5_000


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
                billing_status="written_off",
            )
    data = await report(client, p.owner, p.org, NAME)
    row = row_named(data, "Accounts")
    assert row["staff_value"] == 200_000
    # No client or task rate: the staff charge-out rate is the recovery rate too.
    assert row["recovery_value"] == 200_000 and row["staff_rate_minutes"] == 1_200
    by_person = await report(client, p.owner, p.org, NAME, group_by="person")
    assert row_named(by_person, "Sam Semi-Senior")["staff_value"] == 50_000
    assert row_named(by_person, "Pat Partner")["staff_value"] == 150_000
    assert by_person["totals"]["staff_value"] == 200_000
    assert by_person["totals"]["minutes"] == 1_200


async def test_cost_plus_adds_the_mark_up_of_the_job_else_the_client_else_the_practice(client):
    """A person who costs £50 an hour, billed at cost plus 50%: £75 an hour."""
    p = await new_practice(client)
    job = await make_job(p.org, p.client, "Payroll")
    await make_time_entry(
        p.org,
        p.owner,
        client=p.client,
        job=job,
        minutes=60,
        cost_rate=5_000,
        billing_status="written_off",
    )

    async def payroll(**params) -> dict:
        return row_named(await report(client, p.owner, p.org, NAME, **params), "Payroll")

    row = await payroll()
    assert row["cost_value"] == 5_000 and row["cost_plus_value"] == 7_500
    assert row["markup_bp"] == 5_000
    what_if = await report(client, p.owner, p.org, NAME, markup_bp=2_000)
    assert row_named(what_if, "Payroll")["cost_plus_value"] == 6_000
    assert what_if["filters"]["markup_bp"] == 2_000
    assert what_if["filters"]["practice_markup_bp"] == 5_000

    # The practice's mark-up ...
    response = await client.patch(
        p.url(""), headers=p.owner.headers, json={"cost_plus_markup_bp": 10_000}
    )
    assert response.status_code == 200, response.text
    assert (await payroll())["cost_plus_value"] == 10_000
    # ... the client's wins over it ...
    response = await client.patch(
        p.url(f"/clients/{p.client.id}"),
        headers=p.owner.headers,
        json={"cost_plus_markup_bp": 4_000},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["cost_plus_markup_bp"] == 4_000
    assert (await payroll())["cost_plus_value"] == 7_000
    # ... and the job's wins over the client's; cleared, the client's applies again.
    response = await client.patch(
        p.url(f"/jobs/{job.id}"), headers=p.owner.headers, json={"cost_plus_markup_bp": 2_500}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["cost_plus_markup_bp"] == 2_500
    assert (await payroll())["cost_plus_value"] == 6_250
    await client.patch(
        p.url(f"/jobs/{job.id}"), headers=p.owner.headers, json={"cost_plus_markup_bp": None}
    )
    assert (await payroll())["markup_bp"] == 4_000

    for url, body in (
        ("", {"cost_plus_markup_bp": 100_001}),
        (f"/clients/{p.client.id}", {"cost_plus_markup_bp": -1}),
        (f"/jobs/{job.id}", {"cost_plus_markup_bp": 100_001}),
    ):
        too_much = await client.patch(p.url(url), headers=p.owner.headers, json=body)
        assert too_much.status_code == 422, url


# --- the rate priority ----------------------------------------------------------------------


async def test_the_recovery_rate_follows_the_practice_order(client):
    """£120 agreed with the client for the job, £100 the task rate, £80 the staff rate."""
    p = await new_practice(client)
    await make_staff_rate(p.org, p.owner, 8_000, 3_500)
    service_id = await task_rate(client, p)
    job = await make_job(p.org, p.client, "Accounts", service_id=service_id, hourly_rate=12_000)
    other = await make_job(p.org, p.client, "Advice")
    for target in (job, other):
        await make_time_entry(
            p.org,
            p.owner,
            client=p.client,
            job=target,
            minutes=600,
            billing_status="written_off",
        )

    async def values() -> tuple[int, int, int, int]:
        data = await report(client, p.owner, p.org, NAME)
        row = row_named(data, "Accounts")
        return (
            row["recovery_value"],
            row["client_job_minutes"],
            row["task_minutes"],
            row_named(data, "Advice")["recovery_value"],
        )

    response = await client.get(p.url(""), headers=p.owner.headers)
    organization = response.json()["data"]["organization"]
    assert organization["recovery_rate_order"] == ["client_job", "task", "staff"]
    # Client job rate first; a job without one falls to the next rate that exists (staff).
    assert await values() == (120_000, 600, 0, 80_000)

    async def order(*tiers: str) -> None:
        response = await client.patch(
            p.url(""), headers=p.owner.headers, json={"recovery_rate_order": list(tiers)}
        )
        assert response.status_code == 200, response.text

    await order("task", "client_job", "staff")
    assert await values() == (100_000, 0, 600, 80_000)
    await order("staff", "task", "client_job")
    assert await values() == (80_000, 0, 0, 80_000)
    data = await report(client, p.owner, p.org, NAME)
    assert data["filters"]["rate_order"] == ["staff", "task", "client_job"]
    # The staff rate potential does not depend on the order.
    assert row_named(data, "Accounts")["staff_value"] == 80_000

    for bad in (["task", "staff"], ["task", "task", "staff"], ["client", "task", "staff"]):
        response = await client.patch(
            p.url(""), headers=p.owner.headers, json={"recovery_rate_order": bad}
        )
        assert response.status_code == 422, bad


# --- the invoice value ---------------------------------------------------------------------


async def test_unbilled_hourly_work_is_wip_and_written_off_work_bills_nothing(client):
    p = await new_practice(client)
    await make_staff_rate(p.org, p.owner, 12_000)
    job = await make_job(p.org, p.client, "Bookkeeping")
    entry = await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=60)
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
    assert row["minutes"] == 30 and row["invoice_value"] == 0
    assert row["recovery_value"] == 6_000 and row["recovery_shortfall"] == 6_000
    assert row["wip_minutes"] == 60 and row["wip_value"] == 12_000

    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    await issued(client, p.owner, p.org, invoice["id"])
    row = row_named(await report(client, p.owner, p.org, NAME), "Bookkeeping")
    assert row["minutes"] == 90 and row["invoice_value"] == 12_000
    assert row["recovery_value"] == 18_000 and row["recovery_shortfall"] == 6_000
    assert row["wip_minutes"] == 0


async def test_an_allocated_credit_note_reduces_the_invoice_and_credit_on_account_does_not(
    client,
):
    p = await new_practice(client)
    _, entries = await annual_accounts(client, p)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=entries)
    invoice = await issued(client, p.owner, p.org, invoice["id"])

    # Money paid on account (allocated to no invoice) is a credit for a future invoice.
    response = await pay(client, p.owner, p.org, p.client.id, 20_000)
    assert response.status_code == 201, response.text
    data = await client_recovery(client, p.owner, p)
    assert data["credit_on_account"] == 20_000
    assert data["totals"]["invoice_value"] == 150_000

    # A credit note against the invoice reduces its value.
    response = await client.post(
        p.url(f"/invoices/{invoice['id']}/credit-notes"),
        headers=p.owner.headers,
        json={"lines": [{"description": "Goodwill", "net_amount": 30_000}], "reason": "Goodwill"},
    )
    assert response.status_code == 201, response.text
    row = row_named(await report(client, p.owner, p.org, NAME), "Annual accounts")
    assert (row["invoiced_value"], row["credited_value"], row["invoice_value"]) == (
        150_000,
        30_000,
        120_000,
    )
    assert row["recovery_shortfall"] == 80_000 and row["staff_shortfall"] == 40_000
    data = await client_recovery(client, p.owner, p)
    assert data["totals"]["invoice_value"] == 120_000 and data["credit_on_account"] == 20_000


async def test_rows_by_client_and_the_totals(client):
    p = await new_practice(client)
    _, entries = await annual_accounts(client, p)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=entries)
    await issued(client, p.owner, p.org, invoice["id"])
    data = await report(client, p.owner, p.org, NAME, group_by="client")
    [row] = data["rows"]
    assert row["name"] == "Widgets Ltd" and row["client_id"] == str(p.client.id)
    assert data["totals"]["invoice_value"] == 150_000
    assert data["totals"]["recovery_value"] == 200_000


# --- cost visibility, permissions and downloads ------------------------------------------------


async def test_cost_is_hidden_without_rate_read(client):
    p = await new_practice(client)
    await make_staff_rate(p.org, p.owner, 12_000)
    job = await make_job(p.org, p.client, "Advisory")
    await make_time_entry(
        p.org, p.owner, client=p.client, job=job, minutes=60, billing_status="written_off"
    )
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
    assert row["recovery_value"] == 12_000 and row["staff_value"] == 12_000
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
    _, entries = await annual_accounts(client, p)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=entries)
    await issued(client, p.owner, p.org, invoice["id"])
    response = await download(client, p.owner, p.org, NAME, "csv")
    assert response.status_code == 200, response.text
    table = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    header, first, last = table[0], table[1], table[-1]
    assert header[:3] == ["Job", "Job number", "Client"]
    values = dict(zip(header, first, strict=True))
    assert values["Hours invoiced"] == "20.00"
    assert values["Invoice value"] == "1500.00"
    assert values["Potential recovery"] == "2000.00"
    assert values["Left on table (recovery rate)"] == "500.00"
    assert values["Hours at task rates"] == "20.00"
    assert values["At staff rates"] == "1600.00"
    assert values["Left on table (staff rates)"] == "100.00"
    assert last[0] == "Total"


# --- Cost to Business ------------------------------------------------------------------------


async def test_cost_to_business_is_what_the_practice_pays_for_the_work(client):
    """Twenty hours that cost £35 an hour, invoiced at £1,500, and two non-billable hours."""
    p = await new_practice(client)
    job, entries = await annual_accounts(client, p)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=entries)
    await issued(client, p.owner, p.org, invoice["id"])
    await make_time_entry(
        p.org,
        p.owner,
        client=p.client,
        job=job,
        minutes=120,
        cost_rate=3_500,
        is_billable=False,
        entry_date=MONDAY + timedelta(days=4),
    )
    await make_time_entry(
        p.org,
        p.owner,
        client=p.client,
        job=job,
        minutes=60,
        cost_rate=3_500,
        entry_date=MONDAY + timedelta(days=4),
    )
    data = await report(client, p.owner, p.org, COST)
    row = row_named(data, "Annual accounts")
    assert (row["minutes"], row["billable_minutes"], row["non_billable_minutes"]) == (
        1_380,
        1_260,
        120,
    )
    assert row["cost_value"] == 80_500 and row["cost_per_hour"] == 3_500
    assert (row["invoiced_cost"], row["wip_cost"], row["non_billable_cost"]) == (
        70_000,
        3_500,
        7_000,
    )
    assert row["invoice_value"] == 150_000 and row["margin_value"] == 80_000
    assert data["totals"]["cost_value"] == 80_500
    by_person = await report(client, p.owner, p.org, COST, group_by="person")
    assert by_person["rows"][0]["cost_value"] == 80_500
    response = await download(client, p.owner, p.org, COST, "csv")
    table = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert dict(zip(table[0], table[1], strict=True))["Cost"] == "805.00"


async def test_cost_to_business_needs_rate_read_too(client):
    p = await new_practice(client)
    biller = await staff(p.org, "billing")  # report.financial without rate.read
    response = await fetch(client, biller, p.org, COST)
    assert response.status_code == 403 and error(response)["code"] == "PERMISSION_DENIED"
    manager = await staff(p.org, "manager")
    assert (await fetch(client, manager, p.org, COST)).status_code == 200
    worker = await staff(p.org, "staff")
    assert (await fetch(client, worker, p.org, COST)).status_code == 403


# --- the client's Invoices & Recovery ---------------------------------------------------------


async def test_the_client_view_lists_each_job_and_the_totals(client):
    p = await new_practice(client)
    _, entries = await annual_accounts(client, p)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=entries)
    await issued(client, p.owner, p.org, invoice["id"])
    await make_time_entry(p.org, p.owner, client=p.client, minutes=60, entry_date=MONDAY)
    await make_job(p.org, p.client, "No time yet")
    data = await client_recovery(client, p.owner, p)
    names = [row["name"] for row in data["jobs"]]
    assert names == ["Annual accounts", "Work without a job"]
    accounts = data["jobs"][0]
    assert accounts["terms"] == "Fixed fee of £1,500.00"
    assert accounts["invoice_value"] == 150_000 and accounts["recovery_shortfall"] == 50_000
    assert accounts["staff_shortfall"] == 10_000
    loose = data["jobs"][1]
    assert loose["job_id"] is None and loose["invoice_value"] == 0
    assert loose["wip_minutes"] == 60
    assert data["totals"]["minutes"] == 1_200 and data["totals"]["wip_minutes"] == 60
    assert data["practice_markup_bp"] == 5_000 and data["client_markup_bp"] is None
    assert data["rate_order"] == ["client_job", "task", "staff"]
    assert data["show_cost"] is True and data["credit_on_account"] == 0
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
    await annual_accounts(client, p)
    response = await client.get(
        p.url(f"/clients/{p.client.id}/billing-summary"), headers=p.owner.headers
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    [row] = data["jobs"]
    recovery = row["breakdown"]["recovery"]
    assert recovery == {
        "minutes": 1_200,
        "recovery_value": 200_000,
        "client_job_minutes": 0,
        "task_minutes": 1_200,
        "staff_rate_minutes": 0,
        "staff_value": 160_000,
        "cost_value": 70_000,
        "cost_plus_value": 105_000,
        "markup_bp": 5_000,
    }
    assert data["totals"]["recovery"] == recovery
    assert data["markup_bp"] == 5_000
    assert data["rate_order"] == ["client_job", "task", "staff"]
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
