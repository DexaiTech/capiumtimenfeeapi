"""The expenses report: figures, grouping, filters, who sees whose claims and consistency with the
expense lines list."""

from datetime import UTC, date, datetime

import pytest
from httpx import AsyncClient

from tests.domain_factories import make_client, make_job, practice, staff
from tests.expense_helpers import type_id
from tests.factories import add_member, error
from tests.report_helpers import AUTUMN, SEPTEMBER, call, expense_dataset, get_data
from tests.reports_helpers import custom_role, pin_now
from tests.time_helpers import org_url, set_manager

MEASURES = (
    "line_count",
    "net",
    "vat",
    "gross",
    "billable_net",
    "recharge_amount",
    "recharged_amount",
    "reimbursable",
)


def measures(row: dict) -> tuple:
    return tuple(row[field] for field in MEASURES)


async def test_expenses_by_type(client: AsyncClient) -> None:
    d = await expense_dataset(client)
    data = await get_data(client, d.owner, d.org, "expenses", **AUTUMN)
    assert [r["name"] for r in data["rows"]] == ["Mileage", "Postage", "Subsistence", "Travel"]
    mileage, postage, subsistence, travel = data["rows"]
    # lines, net, vat, gross, billable net, recharge, recharged (invoiced), to reimburse
    assert measures(mileage) == (1, 1_800, 0, 1_800, 1_800, 1_980, 0, 1_800)
    assert measures(postage) == (1, 500, 0, 500, 500, 550, 550, 500)
    assert measures(subsistence) == (1, 1_000, 200, 1_200, 0, 0, 0, 0)  # paid by the practice
    assert measures(travel) == (2, 6_300, 0, 6_300, 4_300, 4_730, 0, 6_300)
    assert travel["expense_type_id"] == str(await type_id(d.org, "TRAVEL"))
    assert (travel["key"], travel["code"]) == (str(await type_id(d.org, "TRAVEL")), "TRAVEL")
    assert measures(data["totals"]) == (5, 9_600, 200, 9_800, 6_600, 7_260, 550, 8_600)
    assert data["filters"]["group_by"] == "type"
    assert data["filters"]["date_from"] == "2026-09-01"
    assert data["filters"]["status"] == []
    assert data["page"] == {
        "total": 4,
        "limit": None,
        "offset": 0,
        "has_more": False,
        "next_offset": None,
    }


async def test_expenses_by_person(client: AsyncClient) -> None:
    d = await expense_dataset(client)
    data = await get_data(client, d.owner, d.org, "expenses", group_by="person", **AUTUMN)
    assert [(r["name"], r["user_id"]) for r in data["rows"]] == [
        ("Ann Adams", str(d.ann.id)),
        ("Mia Manager", str(d.mia.id)),
    ]
    ann, mia = data["rows"]
    assert measures(ann) == (4, 7_800, 200, 8_000, 4_800, 5_280, 550, 6_800)
    assert measures(mia) == (1, 1_800, 0, 1_800, 1_800, 1_980, 0, 1_800)


async def test_expenses_by_client_and_job(client: AsyncClient) -> None:
    d = await expense_dataset(client)
    by_client = await get_data(client, d.owner, d.org, "expenses", group_by="client", **AUTUMN)
    assert [(r["name"], r["client_id"], r["line_count"], r["net"]) for r in by_client["rows"]] == [
        ("Acme Ltd", str(d.acme.id), 2, 4_800),
        ("Beta LLP", str(d.beta.id), 2, 3_800),
        ("No client (overhead)", None, 1, 1_000),
    ]
    assert by_client["rows"][0]["code"] == "ACME"

    by_job = await get_data(client, d.owner, d.org, "expenses", group_by="job", **AUTUMN)
    assert [(r["name"], r["client_name"], r["line_count"]) for r in by_job["rows"]] == [
        ("Accounts 2026", "Acme Ltd", 1),
        ("No job", "Acme Ltd", 1),
        ("No job", "Beta LLP", 2),
        ("No job", None, 1),
    ]
    accounts = by_job["rows"][0]
    assert (accounts["job_id"], accounts["client_id"], accounts["code"]) == (
        str(d.job.id),
        str(d.acme.id),
        "J00001",
    )
    assert by_job["rows"][1]["job_id"] is None
    assert by_job["totals"]["line_count"] == 5


async def test_expenses_by_month_lists_every_month(client: AsyncClient) -> None:
    d = await expense_dataset(client)
    data = await get_data(
        client,
        d.owner,
        d.org,
        "expenses",
        group_by="month",
        date_from=date(2026, 8, 15),
        date_to=date(2026, 10, 10),
    )
    august, september, october = data["rows"]
    assert [(r["key"], r["name"]) for r in data["rows"]] == [
        ("2026-08", "Aug 2026"),
        ("2026-09", "Sep 2026"),
        ("2026-10", "Oct 2026"),
    ]
    # The first and last months are cut to the range, for the drill-down.
    assert (august["date_from"], august["date_to"]) == ("2026-08-15", "2026-08-31")
    assert (september["date_from"], september["date_to"]) == ("2026-09-01", "2026-09-30")
    assert (october["date_from"], october["date_to"]) == ("2026-10-01", "2026-10-10")
    assert measures(august) == (0, 0, 0, 0, 0, 0, 0, 0)
    assert measures(september) == (4, 7_800, 200, 8_000, 4_800, 5_280, 550, 6_800)
    assert measures(october) == (1, 1_800, 0, 1_800, 1_800, 1_980, 0, 1_800)


async def test_expenses_by_claim_status(client: AsyncClient) -> None:
    d = await expense_dataset(client)
    data = await get_data(client, d.owner, d.org, "expenses", group_by="status", **AUTUMN)
    assert [(r["key"], r["name"], r["claim_status"]) for r in data["rows"]] == [
        ("draft", "Draft", "draft"),
        ("submitted", "Submitted", "submitted"),
        ("approved", "Approved", "approved"),
    ]
    assert [r["line_count"] for r in data["rows"]] == [1, 1, 3]
    assert data["rows"][2]["net"] == 5_800


async def test_default_range_is_this_month(client: AsyncClient, monkeypatch) -> None:
    pin_now(monkeypatch, datetime(2026, 9, 30, 12, 0, tzinfo=UTC))
    d = await expense_dataset(client)
    data = await get_data(client, d.owner, d.org, "expenses", group_by="status")
    assert (data["filters"]["date_from"], data["filters"]["date_to"]) == (
        "2026-09-01",
        "2026-09-30",
    )
    assert data["totals"]["line_count"] == 4  # September's four lines; October's is outside


@pytest.mark.parametrize(
    ("params", "lines", "net"),
    [
        ({"status": ["approved"]}, 3, 5_800),
        ({"status": ["approved", "submitted"]}, 4, 7_600),
        ({"status": ["draft"]}, 1, 2_000),
        ({"is_billable": "true"}, 3, 6_600),
        ({"is_billable": "false"}, 2, 3_000),
        ({"billing_status": ["billed"]}, 1, 500),
        ({"billing_status": ["unbilled"]}, 4, 9_100),
        ({"date_from": date(2026, 9, 8), "date_to": date(2026, 9, 9)}, 2, 1_500),
    ],
)
async def test_expenses_filters(client: AsyncClient, params: dict, lines: int, net: int) -> None:
    d = await expense_dataset(client)
    merged = {**AUTUMN, **params}
    data = await get_data(client, d.owner, d.org, "expenses", **merged)
    assert (data["totals"]["line_count"], data["totals"]["net"]) == (lines, net)


async def test_expenses_filters_by_references(client: AsyncClient) -> None:
    d = await expense_dataset(client)
    ask = {"client_id": d.acme.id}
    assert (await get_data(client, d.owner, d.org, "expenses", **AUTUMN, **ask))["totals"][
        "line_count"
    ] == 2
    only_job = await get_data(client, d.owner, d.org, "expenses", **AUTUMN, job_id=d.job.id)
    assert (only_job["totals"]["line_count"], only_job["totals"]["net"]) == (1, 4_300)
    assert only_job["filters"]["job_id"] == str(d.job.id)
    by_ann = await get_data(client, d.owner, d.org, "expenses", **AUTUMN, user_id=d.ann.id)
    assert by_ann["totals"]["line_count"] == 4
    travel = await type_id(d.org, "TRAVEL")
    by_type = await get_data(client, d.owner, d.org, "expenses", **AUTUMN, expense_type_id=travel)
    assert (by_type["totals"]["line_count"], by_type["filters"]["expense_type_id"]) == (
        2,
        str(travel),
    )


async def test_the_report_follows_the_lines_the_screen_lists(client: AsyncClient) -> None:
    """Report figures equal the sums of ``GET /expenses`` for the same filters (2.17)."""
    d = await expense_dataset(client)
    for params in (
        {},
        {"status": ["approved"]},
        {"client_id": str(d.acme.id)},
        {"is_billable": "true"},
        {"billing_status": ["billed"]},
    ):
        listing = await client.get(
            org_url(d.org, "/expenses"),
            headers=d.owner.headers,
            params={"date_from": "2026-09-01", "date_to": "2026-10-31", "limit": 100, **params},
        )
        assert listing.status_code == 200, listing.text
        lines = listing.json()["data"]
        data = await get_data(client, d.owner, d.org, "expenses", **AUTUMN, **params)
        totals = data["totals"]
        assert totals["line_count"] == len(lines), params
        assert totals["net"] == sum(x["net"] for x in lines), params
        assert totals["vat"] == sum(x["vat"] for x in lines), params
        assert totals["gross"] == sum(x["gross"] for x in lines), params
        assert totals["recharge_amount"] == sum(x["recharge_amount"] for x in lines), params
        assert totals["recharged_amount"] == sum(
            x["recharge_amount"] for x in lines if x["billing_status"] == "billed"
        ), params
        assert totals["reimbursable"] == sum(x["gross"] for x in lines if x["payer"] == "staff"), (
            params
        )


# --- Who sees whose claims -----------------------------------------------------------------


async def test_everyone_sees_all_claims_with_read_all(client: AsyncClient) -> None:
    d = await expense_dataset(client)
    billing = await staff(d.org, "billing")  # report.financial and expense.read_all
    for person in (d.owner, d.mia, billing):
        data = await get_data(client, person, d.org, "expenses", **AUTUMN)
        assert data["totals"]["line_count"] == 5, person.full_name


async def test_staff_see_their_own_claims_only(client: AsyncClient) -> None:
    d = await expense_dataset(client)
    data = await get_data(client, d.ann, d.org, "expenses", group_by="person", **AUTUMN)
    assert [(r["name"], r["line_count"]) for r in data["rows"]] == [("Ann Adams", 4)]
    assert data["totals"]["net"] == 7_800
    # Asking for oneself is fine; for a colleague it is a permission matter.
    assert (
        await call(client, d.ann, d.org, "expenses", **AUTUMN, user_id=d.ann.id)
    ).status_code == 200
    refused = await call(client, d.ann, d.org, "expenses", **AUTUMN, user_id=d.mia.id)
    assert refused.status_code == 403
    assert error(refused)["code"] == "PERMISSION_DENIED"
    bob = await staff(d.org, "staff", full_name="Bob Brown")  # no claims
    empty = await get_data(client, bob, d.org, "expenses", **AUTUMN)
    assert empty["rows"] == [] and empty["totals"]["line_count"] == 0


async def test_an_approver_sees_the_people_they_manage(client: AsyncClient) -> None:
    d = await expense_dataset(client)
    await custom_role(d.org, "Approver", {"report.read", "expense.approve"})
    lead = await add_member(d.org["id"], "Approver", full_name="Lena Lead")
    await set_manager(d.org, d.ann, lead)
    data = await get_data(client, lead, d.org, "expenses", group_by="person", **AUTUMN)
    assert [(r["name"], r["line_count"]) for r in data["rows"]] == [("Ann Adams", 4)]
    assert (
        await call(client, lead, d.org, "expenses", **AUTUMN, user_id=d.ann.id)
    ).status_code == 200
    assert (
        await call(client, lead, d.org, "expenses", **AUTUMN, user_id=d.mia.id)
    ).status_code == 403


async def test_a_viewer_has_no_claims_to_report(client: AsyncClient) -> None:
    d = await expense_dataset(client)
    viewer = await staff(d.org, "viewer")  # report.read, no expense permission at all
    data = await get_data(client, viewer, d.org, "expenses", **AUTUMN)
    assert data["rows"] == [] and data["totals"]["gross"] == 0


async def test_the_report_needs_a_report_permission(client: AsyncClient) -> None:
    d = await expense_dataset(client)
    await custom_role(d.org, "Expenses only", {"expense.log", "expense.read_all"})
    person = await add_member(d.org["id"], "Expenses only")
    refused = await call(client, person, d.org, "expenses", **AUTUMN)
    assert refused.status_code == 403
    assert error(refused)["code"] == "PERMISSION_DENIED"
    # Either report permission opens it.
    await custom_role(d.org, "Financial only", {"report.financial"})
    finance = await add_member(d.org["id"], "Financial only")
    assert (await call(client, finance, d.org, "expenses", **AUTUMN)).status_code == 200


async def test_another_practice_answers_404(client: AsyncClient) -> None:
    d = await expense_dataset(client)
    other_owner, other = await practice(client, "Other & Co")
    theirs = await make_client(other, "Theirs")
    their_job = await make_job(other, theirs)
    assert (await call(client, other_owner, d.org, "expenses", **AUTUMN)).status_code == 404
    for params in (
        {"client_id": theirs.id},
        {"job_id": their_job.id},
        {"user_id": other_owner.id},
        {"expense_type_id": await type_id(other, "TRAVEL")},
    ):
        response = await call(client, d.owner, d.org, "expenses", **AUTUMN, **params)
        assert response.status_code == 404, (params, response.text)
        assert error(response)["code"] == "RESOURCE_NOT_FOUND"
    # The tenancy answer comes before the own-claims answer.
    response = await call(client, d.ann, d.org, "expenses", **AUTUMN, user_id=other_owner.id)
    assert response.status_code == 404


async def test_another_practices_claims_never_show(client: AsyncClient) -> None:
    d = await expense_dataset(client)
    other_owner, other = await practice(client, "Other & Co")
    data = await get_data(client, other_owner, other, "expenses", **AUTUMN)
    assert data["rows"] == [] and data["totals"]["line_count"] == 0
    assert (await get_data(client, d.owner, d.org, "expenses", **AUTUMN))["totals"][
        "line_count"
    ] == 5


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"group_by": "planet"}, "query.group_by"),
        ({"status": ["paid"]}, "query.status"),
        ({"billing_status": ["lost"]}, "query.billing_status"),
        ({"is_billable": "maybe"}, "query.is_billable"),
        ({"expense_type_id": "nope"}, "query.expense_type_id"),
        ({"date_from": "2026-09-30", "date_to": "2026-09-01"}, "date_from"),
        ({"date_from": "2010-01-01", "date_to": "2026-01-01"}, "date_to"),
        ({"limit": 0}, "query.limit"),
        ({"limit": 1_001}, "query.limit"),
        ({"offset": -1}, "query.offset"),
        ({"format": "docx"}, "query.format"),
        ({"planet": "mars"}, "query.planet"),
    ],
)
async def test_expenses_validation(client: AsyncClient, params: dict, field: str) -> None:
    owner, org = await practice(client)
    response = await call(client, owner, org, "expenses", **params)
    assert response.status_code == 422, response.text
    body = error(response)
    assert body["code"] == "VALIDATION_ERROR"
    assert any(d["field"].startswith(field) for d in body["details"]), body


async def test_expenses_september_alone(client: AsyncClient) -> None:
    d = await expense_dataset(client)
    data = await get_data(client, d.owner, d.org, "expenses", group_by="client", **SEPTEMBER)
    assert data["totals"]["line_count"] == 4
    assert data["totals"]["net"] == 7_800
