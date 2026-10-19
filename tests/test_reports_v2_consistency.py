"""Report figures equal the figures of the main screens for the same filters (Featurte.md 2.17):
time entries, invoices, WIP, and the reports among themselves."""

from datetime import date

import pytest
from httpx import AsyncClient

from tests.domain_factories import make_client, make_disbursement, make_time_entry, practice
from tests.report_helpers import SEPTEMBER, get_data, wide_dataset
from tests.reports_helpers import LineSpec, make_invoice
from tests.time_helpers import org_url

AS_AT = "2026-10-17"


async def listed(client: AsyncClient, actor, org: dict, path: str, **params) -> list[dict]:
    response = await client.get(
        org_url(org, path), headers=actor.headers, params={"limit": 100, **params}
    )
    assert response.status_code == 200, response.text
    data: list[dict] = response.json()["data"]
    return data


@pytest.mark.parametrize("scope", ["all", "client", "job", "person"])
async def test_time_summary_equals_the_time_entries_list(client: AsyncClient, scope: str) -> None:
    d = await wide_dataset(client)
    # Time that is not billable, and time outside the range: in the list, not in the figures.
    await make_time_entry(
        d.org, d.owner, entry_date=date(2026, 9, 9), minutes=30, is_billable=False
    )
    await make_time_entry(d.org, d.sam, client=d.alpha, job=d.a1, entry_date=date(2026, 8, 31))
    filters = {
        "all": {},
        "client": {"client_id": d.alpha.id},
        "job": {"job_id": d.b1.id},
        "person": {"user_id": d.sam.id},
    }[scope]
    entries = await listed(
        client,
        d.owner,
        d.org,
        "/time-entries",
        user_id=str(filters.get("user_id", "all")),
        date_from="2026-09-01",
        date_to="2026-09-30",
        **{k: str(v) for k, v in filters.items() if k != "user_id"},
    )
    data = await get_data(client, d.owner, d.org, "time-summary", **SEPTEMBER, **filters)
    totals = data["totals"]
    billable = [e for e in entries if e["is_billable"]]
    assert totals["entry_count"] == len(entries)
    assert totals["total_minutes"] == sum(e["minutes"] for e in entries)
    assert totals["chargeable_minutes"] == sum(e["minutes"] for e in billable)
    assert totals["charge_value"] == sum(e["charge_value"] for e in billable)
    assert totals["cost_value"] == sum(e["cost_value"] for e in entries)


async def test_the_time_reports_agree_with_each_other(client: AsyncClient) -> None:
    d = await wide_dataset(client)
    by_job = await get_data(client, d.owner, d.org, "time-summary", group_by="job", **SEPTEMBER)
    minutes = {r["job_id"]: r["total_minutes"] for r in by_job["rows"]}
    jobs = await get_data(client, d.owner, d.org, "job-profitability", **SEPTEMBER)
    assert {r["job_id"]: r["logged_minutes"] for r in jobs["rows"]} == minutes
    people = await get_data(client, d.owner, d.org, "utilisation", **SEPTEMBER)
    summary = await get_data(client, d.owner, d.org, "time-summary", **SEPTEMBER)
    assert people["totals"]["total_minutes"] == summary["totals"]["total_minutes"]
    assert people["totals"]["chargeable_minutes"] == summary["totals"]["chargeable_minutes"]


async def test_billing_summary_equals_the_invoices_list(client: AsyncClient) -> None:
    d = await wide_dataset(client)
    # A draft and a void invoice show in the list; neither is billing.
    await make_invoice(d.org, d.alpha, [LineSpec(7_777)], issue_date=None, status="draft")
    await make_invoice(
        d.org, d.beta, [LineSpec(9_999)], issue_date=date(2026, 9, 12), status="void"
    )
    invoices = await listed(
        client, d.owner, d.org, "/invoices", date_from="2026-09-01", date_to="2026-09-30"
    )
    issued = [i for i in invoices if i["display_status"] not in ("draft", "void")]
    assert len(issued) == 3
    data = await get_data(client, d.owner, d.org, "billing-summary", **SEPTEMBER)
    totals = data["totals"]
    assert totals["invoice_count"] == len(issued)
    assert totals["invoiced_net"] == sum(i["net_total"] for i in issued)
    assert totals["invoiced_vat"] == sum(i["vat_total"] for i in issued)
    assert totals["invoiced_gross"] == sum(i["gross_total"] for i in issued)
    vat = await get_data(client, d.owner, d.org, "vat-summary", **SEPTEMBER)
    assert vat["totals"]["invoiced_net"] == totals["invoiced_net"]
    assert vat["totals"]["invoiced_vat"] == totals["invoiced_vat"]


async def test_aged_debtors_equal_the_outstanding_invoices(client: AsyncClient) -> None:
    d = await wide_dataset(client)
    await make_invoice(
        d.org,
        d.alpha,
        [LineSpec(3_000, vat_bp=0)],
        issue_date=date(2026, 6, 1),
        due_date=date(2026, 6, 30),
    )
    outstanding = await listed(client, d.owner, d.org, "/invoices", outstanding="true")
    data = await get_data(client, d.owner, d.org, "aged-debtors", as_at=AS_AT)
    assert data["totals"]["invoice_count"] == len(outstanding) == 4
    assert data["totals"]["total"] == sum(i["balance"] for i in outstanding)
    # Alpha's invoice is part paid, so its balance is less than its gross.
    assert data["totals"]["total"] < sum(i["gross_total"] for i in outstanding)
    by_client = {r["client_code"]: r["total"] for r in data["rows"]}
    alpha = await listed(
        client, d.owner, d.org, "/invoices", outstanding="true", client_id=str(d.alpha.id)
    )
    assert by_client["ALPHA"] == sum(i["balance"] for i in alpha)


async def test_wip_report_equals_the_wip_screen(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd", code="ACME")
    beta = await make_client(org, "Beta LLP", code="BETA")
    await make_time_entry(org, owner, client=acme, entry_date=date(2026, 9, 7))  # 120.00
    await make_disbursement(org, acme, 1_300, incurred_on=date(2026, 9, 8))
    await make_time_entry(org, owner, client=beta, entry_date=date(2026, 9, 9), minutes=30)  # 60.00
    await make_time_entry(  # not WIP: already billed
        org, owner, client=beta, entry_date=date(2026, 9, 10), billing_status="billed"
    )
    screen = await listed(client, owner, org, "/wip")
    report = await get_data_wip(client, owner, org)
    assert report["totals"]["total"] == sum(r["total"] for r in screen) == 12_000 + 1_300 + 6_000
    assert report["totals"]["time_value"] == sum(r["time_value"] for r in screen)
    assert report["totals"]["disbursement_value"] == sum(r["disbursement_value"] for r in screen)
    by_client = {r["client_code"]: r["total"] for r in report["rows"]}
    assert by_client == {r["client"]["code"]: r["total"] for r in screen}


async def get_data_wip(client: AsyncClient, actor, org: dict) -> dict:
    return await get_data(client, actor, org, "wip", as_at="2026-10-17")
