"""Report permissions, tenant isolation, validation and CSV output."""

import asyncio
import csv
import io
from datetime import date

import pytest
from httpx import AsyncClient

from tests.domain_factories import make_client, make_job, make_time_entry, practice, staff
from tests.factories import add_member, error
from tests.reports_helpers import (
    LineSpec,
    allocate,
    custom_role,
    fetch,
    make_invoice,
    report,
)

FINANCIAL = (
    "wip",
    "aged-debtors",
    "recoverability",
    "billing-summary",
    "job-profitability",
    "vat-summary",
)
READ = ("time-summary", "utilisation")
ALL = FINANCIAL + READ


@pytest.mark.parametrize("name", FINANCIAL)
async def test_financial_reports_need_report_financial(client: AsyncClient, name: str) -> None:
    _, org = await practice(client)
    for role in ("viewer", "staff"):
        member = await staff(org, role)
        response = await fetch(client, member, org, name)
        assert response.status_code == 403, (role, response.text)
        assert error(response)["code"] == "PERMISSION_DENIED"
    for role in ("manager", "billing", "partner", "admin"):
        member = await staff(org, role)
        assert (await fetch(client, member, org, name)).status_code == 200, role


@pytest.mark.parametrize("name", READ)
async def test_time_reports_need_report_read(client: AsyncClient, name: str) -> None:
    _, org = await practice(client)
    await custom_role(org, "No reports", {"time.log", "time.read_all"})
    outsider = await add_member(org["id"], "No reports")
    response = await fetch(client, outsider, org, name)
    assert response.status_code == 403
    for role in ("viewer", "staff"):
        assert (await fetch(client, await staff(org, role), org, name)).status_code == 200


@pytest.mark.parametrize("name", ALL)
async def test_another_practice_answers_404(client: AsyncClient, name: str) -> None:
    _, org = await practice(client)
    other_owner, _ = await practice(client, "Other & Co")
    response = await fetch(client, other_owner, org, name)
    assert response.status_code == 404
    assert error(response)["code"] == "RESOURCE_NOT_FOUND"


async def test_filter_ids_of_another_practice_answer_404(client: AsyncClient) -> None:
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other & Co")
    their_client = await make_client(other, "Theirs")
    their_job = await make_job(other, their_client)
    cases = [
        ("wip", {"client_id": their_client.id}),
        ("wip", {"job_id": their_job.id}),
        ("wip", {"partner_user_id": other_owner.id}),
        ("aged-debtors", {"manager_user_id": other_owner.id}),
        ("time-summary", {"user_id": other_owner.id}),
        ("utilisation", {"manager_user_id": other_owner.id}),
        ("recoverability", {"client_id": their_client.id}),
        ("billing-summary", {"client_id": their_client.id}),
        ("job-profitability", {"job_id": their_job.id}),
        ("vat-summary", {"client_id": their_client.id}),
    ]
    for name, params in cases:
        response = await fetch(client, owner, org, name, **params)
        assert response.status_code == 404, (name, params, response.text)


@pytest.mark.parametrize("name", READ)
async def test_other_practice_user_id_answers_404_without_read_all(
    client: AsyncClient, name: str
) -> None:
    """The own-time rule (403) must not mask the tenancy rule (404, never 403)."""
    _, org = await practice(client)
    other_owner, _ = await practice(client, "Other & Co")
    member = await staff(org, "staff")
    colleague = await staff(org, "staff")
    response = await fetch(client, member, org, name, user_id=other_owner.id)
    assert response.status_code == 404, response.text
    assert error(response)["code"] == "RESOURCE_NOT_FOUND"
    # A colleague in the same practice is a permission matter.
    assert (await fetch(client, member, org, name, user_id=colleague.id)).status_code == 403


async def test_another_practices_data_never_shows(client: AsyncClient) -> None:
    owner, org = await practice(client)
    other_owner, other = await practice(client, "Other & Co")
    theirs = await make_client(other, "Theirs")
    entry = await make_time_entry(other, other_owner, client=theirs, entry_date=date(2026, 9, 7))
    made = await make_invoice(other, theirs, [LineSpec(5_000)], issue_date=date(2026, 9, 8))
    await make_time_entry(other, other_owner, client=theirs, entry_date=date(2026, 9, 9))
    await allocate(other, made, entry)
    window = {"date_from": date(2026, 9, 1), "date_to": date(2026, 9, 30)}
    assert (await report(client, owner, org, "wip", as_at=date(2026, 10, 1)))["rows"] == []
    assert (await report(client, owner, org, "aged-debtors", as_at=date(2026, 10, 1)))["rows"] == []
    assert (await report(client, owner, org, "time-summary", **window))["rows"] == []
    assert (await report(client, owner, org, "recoverability", **window))["rows"] == []
    assert (await report(client, owner, org, "job-profitability", **window))["rows"] == []
    assert (await report(client, owner, org, "vat-summary", **window))["rows"] == []
    billing = await report(client, owner, org, "billing-summary", **window)
    assert billing["totals"]["invoice_count"] == 0
    utilisation = await report(client, owner, org, "utilisation", **window)
    assert [r["user_id"] for r in utilisation["rows"]] == [str(owner.id)]


@pytest.mark.parametrize(
    ("name", "params", "field"),
    [
        ("time-summary", {"date_from": "2026-09-30", "date_to": "2026-09-01"}, "date_from"),
        ("time-summary", {"date_from": "2010-01-01", "date_to": "2026-01-01"}, "date_to"),
        ("utilisation", {"date_from": "2030-01-01"}, "date_from"),  # after today
        ("job-profitability", {"date_from": "2026-09-30", "date_to": "2026-09-01"}, "date_from"),
        ("time-summary", {"group_by": "planet"}, "query.group_by"),
        ("recoverability", {"group_by": "week"}, "query.group_by"),
        ("billing-summary", {"group_by": "client"}, "query.group_by"),
        ("wip", {"as_at": "not-a-date"}, "query.as_at"),
        ("wip", {"client_id": "nope"}, "query.client_id"),
        ("wip", {"format": "pdf"}, "query.format"),
        ("aged-debtors", {"job_id": "8d3c2a7e-0000-4000-8000-000000000000"}, "query.job_id"),
    ],
)
async def test_validation(client: AsyncClient, name: str, params: dict, field: str) -> None:
    owner, org = await practice(client)
    response = await fetch(client, owner, org, name, **params)
    assert response.status_code == 422, response.text
    body = error(response)
    assert body["code"] == "VALIDATION_ERROR"
    assert field in [d["field"] for d in body["details"]], body


async def test_wip_csv(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "=HYPERLINK(evil)", code="ACME")
    job = await make_job(org, acme, "Accounts, 2026", number="J00001")
    await make_time_entry(
        org,
        owner,
        client=acme,
        job=job,
        entry_date=date(2026, 10, 1),
        minutes=7,
        charge_rate=10_000,
    )
    await make_time_entry(
        org, owner, client=acme, job=job, entry_date=date(2026, 10, 2), minutes=95
    )
    response = await fetch(client, owner, org, "wip", as_at=date(2026, 10, 17), format="csv")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"] == 'attachment; filename="wip_2026-10-17.csv"'
    assert response.content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == [
        "Client code",
        "Client",
        "Job number",
        "Job",
        "Hours",
        "Time value",
        "Disbursements",
        "Total WIP",
        "0-30 days",
        "31-60 days",
        "61-90 days",
        "91+ days",
        "Oldest item",
    ]
    # 102 minutes = 1.70 h; 11.67 + 190.00 = 201.67.
    assert rows[1] == [
        "ACME",
        "'=HYPERLINK(evil)",
        "J00001",
        "Accounts, 2026",
        "1.70",
        "201.67",
        "0.00",
        "201.67",
        "201.67",
        "0.00",
        "0.00",
        "0.00",
        "2026-10-01",
    ]
    assert rows[2] == [
        "Total",
        "",
        "",
        "",
        "1.70",
        "201.67",
        "0.00",
        "201.67",
        "201.67",
        "0.00",
        "0.00",
        "0.00",
        "",
    ]
    assert len(rows) == 3
    assert b"\r\n" in response.content


async def test_csv_negative_amounts_percentages_and_names(client: AsyncClient) -> None:
    owner, org = await practice(client, full_name="Olivia Owner")
    acme = await make_client(org, "Acme", code="ACME")
    entry = await make_time_entry(org, owner, client=acme, entry_date=date(2026, 9, 7))
    made = await make_invoice(org, acme, [LineSpec(9_005)], issue_date=date(2026, 9, 8))
    await allocate(org, made, entry, 9_005)
    response = await fetch(
        client,
        owner,
        org,
        "recoverability",
        date_from=date(2026, 9, 1),
        date_to=date(2026, 9, 30),
        group_by="client",
        format="csv",
    )
    assert (
        response.headers["content-disposition"]
        == 'attachment; filename="recoverability_2026-09-01_2026-09-30.csv"'
    )
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0][:3] == ["Client", "Client code", "WIP value billed"]
    # 120.00 billed at 90.05: a write-down of 29.95; recovery 75.04%.
    assert rows[1] == [
        "Acme",
        "ACME",
        "120.00",
        "90.05",
        "-29.95",
        "0.00",
        "120.00",
        "-29.95",
        "75.04",
    ]
    assert rows[2][0] == "Total"


@pytest.mark.parametrize("name", ALL)
async def test_every_report_renders_csv(client: AsyncClient, name: str) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme")
    job = await make_job(org, acme, "Accounts")
    entry = await make_time_entry(org, owner, client=acme, job=job, entry_date=date(2026, 9, 7))
    made = await make_invoice(org, acme, [LineSpec(12_000)], issue_date=date(2026, 9, 8))
    await allocate(org, made, entry)
    params = {"format": "csv"}
    if name in ("wip", "aged-debtors"):
        params["as_at"] = "2026-10-17"
    else:
        params |= {"date_from": "2026-09-01", "date_to": "2026-09-30"}
    response = await fetch(client, owner, org, name, **params)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) >= 2
    assert rows[-1][0] == "Total"
    assert all(len(row) == len(rows[0]) for row in rows)


async def test_reports_run_concurrently(client: AsyncClient) -> None:
    owner, org = await practice(client)
    acme = await make_client(org, "Acme")
    await make_time_entry(org, owner, client=acme, entry_date=date(2026, 9, 7))
    responses = await asyncio.gather(
        *(fetch(client, owner, org, "wip", as_at=date(2026, 10, 17)) for _ in range(5)),
        *(fetch(client, owner, org, "time-summary", date_from="2026-09-01") for _ in range(5)),
    )
    assert all(r.status_code == 200 for r in responses)
    assert {r.json()["data"]["totals"]["total"] for r in responses[:5]} == {12_000}


async def test_openapi_documents_the_reports(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    base = "/api/v1/organizations/{organization_id}"
    for name in ALL:
        operation = schema["paths"][f"{base}/reports/{name}"]["get"]
        assert operation["summary"]
        assert "text/csv" in operation["responses"]["200"]["content"]
        assert {"403", "404", "422"} <= set(operation["responses"])
    assert schema["paths"][f"{base}/dashboard"]["get"]["summary"] == "Dashboard"
