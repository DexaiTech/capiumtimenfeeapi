"""Paging, drill-down ids and the 90+ ageing bucket of the existing reports, and the OpenAPI
description of every report parameter."""

from datetime import date

import pytest
from httpx import AsyncClient

from tests.domain_factories import make_client
from tests.factories import error
from tests.report_helpers import SEPTEMBER, call, get_data, wide_dataset
from tests.reports_helpers import LineSpec, make_invoice

AS_AT = "2026-10-17"
PAGED = [
    ("aged-debtors", {"as_at": AS_AT}),
    ("time-summary", {"group_by": "day", **SEPTEMBER}),
    ("utilisation", SEPTEMBER),
    ("recoverability", {"group_by": "client", **SEPTEMBER}),
    ("billing-summary", {"group_by": "month", "date_from": "2026-06-01", "date_to": "2026-10-31"}),
    ("job-profitability", SEPTEMBER),
    ("vat-summary", SEPTEMBER),
]


@pytest.mark.parametrize(("name", "params"), PAGED)
async def test_pages_add_up_to_the_report(client: AsyncClient, name: str, params: dict) -> None:
    d = await wide_dataset(client)
    whole = await get_data(client, d.owner, d.org, name, **params)
    count = len(whole["rows"])
    assert count >= 3, name
    # Without a limit every row is returned and the page says so.
    assert whole["page"] == {
        "total": count,
        "limit": None,
        "offset": 0,
        "has_more": False,
        "next_offset": None,
    }
    rows: list[dict] = []
    offset = 0
    while True:
        page = await get_data(client, d.owner, d.org, name, limit=2, offset=offset, **params)
        assert page["totals"] == whole["totals"], name  # totals cover the whole report
        assert page["filters"] == whole["filters"], name
        assert len(page["rows"]) <= 2
        assert (page["page"]["total"], page["page"]["limit"], page["page"]["offset"]) == (
            count,
            2,
            offset,
        )
        rows += page["rows"]
        if not page["page"]["has_more"]:
            assert page["page"]["next_offset"] is None
            break
        assert page["page"]["next_offset"] == offset + 2
        offset = page["page"]["next_offset"]
    assert rows == whole["rows"], name


@pytest.mark.parametrize(("name", "params"), PAGED)
async def test_a_page_past_the_end_is_empty(client: AsyncClient, name: str, params: dict) -> None:
    d = await wide_dataset(client)
    data = await get_data(client, d.owner, d.org, name, limit=5, offset=500, **params)
    assert data["rows"] == []
    assert data["page"]["has_more"] is False and data["page"]["next_offset"] is None
    assert data["totals"]  # the totals are still the report's


async def test_a_limit_above_the_rows_returns_them_all(client: AsyncClient) -> None:
    d = await wide_dataset(client)
    data = await get_data(client, d.owner, d.org, "job-profitability", limit=1_000, **SEPTEMBER)
    assert len(data["rows"]) == 3 and data["page"]["has_more"] is False


@pytest.mark.parametrize(
    ("name", "param", "value"),
    [
        ("time-summary", "limit", 0),
        ("time-summary", "limit", 1_001),
        ("time-summary", "offset", -1),
        ("aged-debtors", "limit", "many"),
        ("job-profitability", "offset", "x"),
        ("utilisation", "limit", -3),
    ],
)
async def test_paging_parameters_are_validated(
    client: AsyncClient, name: str, param: str, value: object
) -> None:
    d = await wide_dataset(client)
    response = await call(client, d.owner, d.org, name, **{param: value})
    assert response.status_code == 422, response.text
    assert error(response)["details"][0]["field"] == f"query.{param}"


# --- Drill-down ids ------------------------------------------------------------------------


async def test_time_summary_rows_name_their_group(client: AsyncClient) -> None:
    d = await wide_dataset(client)

    def only(rows: list[dict], *fields: str) -> list[dict]:
        """The id fields ``fields`` of the rows; every other id field must be null."""
        ids = ("user_id", "client_id", "job_id", "activity_code_id", "service_id")
        for row in rows:
            assert all(row[f] is None for f in ids if f not in fields), row
        return rows

    people = await get_data(client, d.owner, d.org, "time-summary", **SEPTEMBER)
    assert {r["user_id"] for r in only(people["rows"], "user_id")} == {
        str(d.owner.id),
        str(d.sam.id),
    }
    clients = await get_data(client, d.owner, d.org, "time-summary", group_by="client", **SEPTEMBER)
    assert {r["client_id"] for r in only(clients["rows"], "client_id")} == {
        str(d.alpha.id),
        str(d.beta.id),
        str(d.gamma.id),
    }
    jobs = await get_data(client, d.owner, d.org, "time-summary", group_by="job", **SEPTEMBER)
    assert {(r["job_id"], r["client_id"]) for r in only(jobs["rows"], "job_id", "client_id")} == {
        (str(d.a1.id), str(d.alpha.id)),
        (str(d.b1.id), str(d.beta.id)),
        (str(d.g1.id), str(d.gamma.id)),
    }
    activities = await get_data(
        client, d.owner, d.org, "time-summary", group_by="activity", **SEPTEMBER
    )
    assert {r["activity_code_id"] for r in only(activities["rows"], "activity_code_id")} == {
        str(d.acc_id),
        None,  # time with no activity code
    }
    services = await get_data(
        client, d.owner, d.org, "time-summary", group_by="service", **SEPTEMBER
    )
    assert {r["service_id"] for r in only(services["rows"], "service_id")} == {
        str(d.accs_id),
        None,
    }


async def test_time_summary_buckets_carry_their_days_cut_to_the_range(
    client: AsyncClient,
) -> None:
    d = await wide_dataset(client)
    window = {"date_from": date(2026, 9, 2), "date_to": date(2026, 9, 16)}

    def days(data: dict) -> list[tuple]:
        return [(r["key"], r["date_from"], r["date_to"]) for r in data["rows"]]

    by_day = await get_data(client, d.owner, d.org, "time-summary", group_by="day", **window)
    assert days(by_day) == [
        ("2026-09-07", "2026-09-07", "2026-09-07"),
        ("2026-09-08", "2026-09-08", "2026-09-08"),
        ("2026-09-14", "2026-09-14", "2026-09-14"),
        ("2026-09-15", "2026-09-15", "2026-09-15"),
    ]
    by_week = await get_data(client, d.owner, d.org, "time-summary", group_by="week", **window)
    assert days(by_week) == [
        ("2026-09-07", "2026-09-07", "2026-09-13"),
        ("2026-09-14", "2026-09-14", "2026-09-16"),  # the last week stops at the range's end
    ]
    by_month = await get_data(client, d.owner, d.org, "time-summary", group_by="month", **window)
    assert days(by_month) == [("2026-09-01", "2026-09-02", "2026-09-16")]  # starts at the range
    # Rows of other groupings have no days of their own.
    by_person = await get_data(client, d.owner, d.org, "time-summary", **window)
    assert all(r["date_from"] is None and r["date_to"] is None for r in by_person["rows"])


async def test_recoverability_rows_name_their_group(client: AsyncClient) -> None:
    d = await wide_dataset(client)
    by_client = await get_data(client, d.owner, d.org, "recoverability", **SEPTEMBER)
    assert [(r["key"], r["client_id"], r["job_id"], r["user_id"]) for r in by_client["rows"]] == [
        (str(c.id), str(c.id), None, None) for c in (d.alpha, d.beta, d.gamma)
    ]
    by_job = await get_data(client, d.owner, d.org, "recoverability", group_by="job", **SEPTEMBER)
    # Sorted by job name: Accounts, Payroll, VAT Q3.
    assert [(r["job_id"], r["client_id"], r["user_id"]) for r in by_job["rows"]] == [
        (str(job.id), str(c.id), None)
        for job, c in ((d.a1, d.alpha), (d.g1, d.gamma), (d.b1, d.beta))
    ]
    by_person = await get_data(
        client, d.owner, d.org, "recoverability", group_by="person", **SEPTEMBER
    )
    assert {r["user_id"] for r in by_person["rows"]} == {str(d.owner.id), str(d.sam.id)}
    assert all(r["client_id"] is None and r["job_id"] is None for r in by_person["rows"])


async def test_billing_summary_rows_name_their_group(client: AsyncClient) -> None:
    d = await wide_dataset(client)
    months = await get_data(
        client,
        d.owner,
        d.org,
        "billing-summary",
        date_from=date(2026, 8, 15),
        date_to=date(2026, 10, 10),
    )
    assert [(r["key"], r["date_from"], r["date_to"]) for r in months["rows"]] == [
        ("2026-08", "2026-08-15", "2026-08-31"),
        ("2026-09", "2026-09-01", "2026-09-30"),
        ("2026-10", "2026-10-01", "2026-10-10"),
    ]
    assert all(r["partner_user_id"] is None for r in months["rows"])
    partners = await get_data(
        client, d.owner, d.org, "billing-summary", group_by="partner", **SEPTEMBER
    )
    assert [(r["name"], r["partner_user_id"]) for r in partners["rows"]] == [
        ("Pat Partner", str(d.pat.id)),
        ("No partner", None),
    ]
    assert all(r["date_from"] is None for r in partners["rows"])


async def test_rows_that_already_had_ids_keep_them(client: AsyncClient) -> None:
    d = await wide_dataset(client)
    debtors = await get_data(client, d.owner, d.org, "aged-debtors", as_at=AS_AT)
    assert {r["client_id"] for r in debtors["rows"]} == {
        str(d.alpha.id),
        str(d.beta.id),
        str(d.gamma.id),
    }
    jobs = await get_data(client, d.owner, d.org, "job-profitability", **SEPTEMBER)
    assert {(r["job_id"], r["client_id"]) for r in jobs["rows"]} == {
        (str(d.a1.id), str(d.alpha.id)),
        (str(d.b1.id), str(d.beta.id)),
        (str(d.g1.id), str(d.gamma.id)),
    }
    utilisation = await get_data(client, d.owner, d.org, "utilisation", **SEPTEMBER)
    assert {r["user_id"] for r in utilisation["rows"]} == {
        str(d.owner.id),
        str(d.sam.id),
        str(d.pat.id),
    }


# --- The 90+ bucket ------------------------------------------------------------------------


async def test_aged_debtors_has_the_spec_90_plus_bucket(client: AsyncClient) -> None:
    d = await wide_dataset(client)
    old = await make_client(d.org, "Old Debts Ltd", code="OLD")
    # Due 109 days before the report date; another due exactly 90 days before (61-90).
    await make_invoice(
        d.org,
        old,
        [LineSpec(7_000, vat_bp=0)],
        issue_date=date(2026, 6, 1),
        due_date=date(2026, 6, 30),
    )
    await make_invoice(
        d.org,
        old,
        [LineSpec(3_000, vat_bp=0)],
        issue_date=date(2026, 7, 1),
        due_date=date(2026, 7, 19),
    )
    data = await get_data(client, d.owner, d.org, "aged-debtors", as_at=AS_AT)
    row = next(r for r in data["rows"] if r["client_code"] == "OLD")
    assert (row["days_61_90"], row["days_91_plus"], row["days_90_plus"]) == (3_000, 7_000, 7_000)
    assert row["total"] == 10_000
    # The old field stays; both read the same in the totals and in a CSV download.
    assert data["totals"]["days_90_plus"] == data["totals"]["days_91_plus"] == 7_000
    for r in data["rows"]:
        assert r["days_90_plus"] == r["days_91_plus"]


# --- OpenAPI -------------------------------------------------------------------------------


async def test_every_report_parameter_is_described(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    base = "/api/v1/organizations/{organization_id}/reports"
    names = (
        "aged-debtors",
        "time-summary",
        "utilisation",
        "recoverability",
        "billing-summary",
        "job-profitability",
        "vat-summary",
        "expenses",
        "payments",
        "missing-timesheets",
    )
    for name in names:
        operation = schema["paths"][f"{base}/{name}"]["get"]
        assert operation["summary"] and operation["description"]
        parameters = {p["name"]: p for p in operation["parameters"] if p["in"] == "query"}
        assert {"format", "limit", "offset"} <= set(parameters), name
        for parameter in parameters.values():
            assert parameter.get("description"), (name, parameter["name"])
        content = operation["responses"]["200"]["content"]
        assert {"text/csv", "application/pdf", "application/json"} <= set(content), name
        assert any("spreadsheetml" in media for media in content), name
        assert {"403", "404", "422"} <= set(operation["responses"]), name
