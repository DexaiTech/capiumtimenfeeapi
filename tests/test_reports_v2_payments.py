"""The payments report: received, allocated, on account and void/reversed, by month, client and
method, and its consistency with the billing summary and the payments list."""

from datetime import date

import pytest
from httpx import AsyncClient

from tests.domain_factories import make_client, practice, staff
from tests.factories import error
from tests.report_helpers import AUTUMN, call, get_data, payment_dataset
from tests.time_helpers import org_url

FIELDS = ("payment_count", "received", "allocated", "on_account", "void_count", "void_amount")


def figures(row: dict) -> tuple:
    return tuple(row[field] for field in FIELDS)


async def test_payments_by_month(client: AsyncClient) -> None:
    d = await payment_dataset(client)
    data = await get_data(client, d.owner, d.org, "payments", **AUTUMN)
    september, october = data["rows"]
    assert [(r["key"], r["name"]) for r in data["rows"]] == [
        ("2026-09", "Sep 2026"),
        ("2026-10", "Oct 2026"),
    ]
    # payments, received, allocated, on account, void count, void amount
    assert figures(september) == (2, 15_000, 11_000, 4_000, 1, 1_000)
    assert figures(october) == (1, 2_500, 0, 2_500, 0, 0)
    assert figures(data["totals"]) == (3, 17_500, 11_000, 6_500, 1, 1_000)
    assert (september["date_from"], september["date_to"]) == ("2026-09-01", "2026-09-30")
    assert data["filters"]["group_by"] == "month"
    assert data["page"]["total"] == 2


async def test_every_month_of_the_range_is_listed(client: AsyncClient) -> None:
    d = await payment_dataset(client)
    data = await get_data(
        client,
        d.owner,
        d.org,
        "payments",
        date_from=date(2026, 8, 15),
        date_to=date(2026, 10, 10),
    )
    assert [r["key"] for r in data["rows"]] == ["2026-08", "2026-09", "2026-10"]
    august = data["rows"][0]
    assert figures(august) == (0, 0, 0, 0, 0, 0)
    assert (august["date_from"], august["date_to"]) == ("2026-08-15", "2026-08-31")
    assert data["rows"][2]["date_to"] == "2026-10-10"


async def test_payments_by_client(client: AsyncClient) -> None:
    d = await payment_dataset(client)
    data = await get_data(client, d.owner, d.org, "payments", group_by="client", **AUTUMN)
    acme, beta = data["rows"]
    assert (acme["name"], acme["code"], acme["client_id"]) == ("Acme Ltd", "ACME", str(d.acme.id))
    assert figures(acme) == (2, 12_500, 6_000, 6_500, 1, 1_000)
    assert (beta["name"], beta["client_id"]) == ("Beta LLP", str(d.beta.id))
    assert figures(beta) == (1, 5_000, 5_000, 0, 0, 0)
    assert figures(data["totals"]) == (3, 17_500, 11_000, 6_500, 1, 1_000)


async def test_payments_by_method(client: AsyncClient) -> None:
    d = await payment_dataset(client)
    data = await get_data(client, d.owner, d.org, "payments", group_by="method", **AUTUMN)
    assert [(r["key"], r["method"], r["name"]) for r in data["rows"]] == [
        ("bank_transfer", "bank_transfer", "Bank transfer"),
        ("card", "card", "Card"),
        ("cash", "cash", "Cash"),
        ("cheque", "cheque", "Cheque"),
    ]
    assert [figures(r) for r in data["rows"]] == [
        (1, 10_000, 6_000, 4_000, 0, 0),
        (1, 5_000, 5_000, 0, 0, 0),
        (1, 2_500, 0, 2_500, 0, 0),
        (0, 0, 0, 0, 1, 1_000),  # only a voided cheque: nothing received
    ]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"method": "cash"}, (1, 2_500, 0, 2_500, 0, 0)),
        ({"method": "cheque"}, (0, 0, 0, 0, 1, 1_000)),
        (
            {"date_from": date(2026, 9, 10), "date_to": date(2026, 9, 30)},
            (1, 5_000, 5_000, 0, 1, 1_000),
        ),
    ],
)
async def test_payments_filters(client: AsyncClient, params: dict, expected: tuple) -> None:
    d = await payment_dataset(client)
    data = await get_data(client, d.owner, d.org, "payments", **{**AUTUMN, **params})
    assert figures(data["totals"]) == expected


async def test_payments_filtered_by_client_and_partner(client: AsyncClient) -> None:
    d = await payment_dataset(client)
    only_acme = await get_data(client, d.owner, d.org, "payments", client_id=d.acme.id, **AUTUMN)
    assert figures(only_acme["totals"]) == (2, 12_500, 6_000, 6_500, 1, 1_000)
    assert only_acme["filters"]["client_id"] == str(d.acme.id)
    # Pat is Acme's partner; Beta has none.
    by_partner = await get_data(
        client, d.owner, d.org, "payments", partner_user_id=d.partner.id, **AUTUMN
    )
    assert figures(by_partner["totals"]) == figures(only_acme["totals"])
    assert by_partner["filters"]["partner_user_id"] == str(d.partner.id)


async def test_payments_equal_the_billing_summary_and_the_list(client: AsyncClient) -> None:
    """Report figures equal what the other screens show for the same filters (2.17)."""
    d = await payment_dataset(client)
    for params in ({}, {"client_id": str(d.acme.id)}, {"client_id": str(d.beta.id)}):
        report = await get_data(client, d.owner, d.org, "payments", **AUTUMN, **params)
        billing = await get_data(client, d.owner, d.org, "billing-summary", **AUTUMN, **params)
        assert report["totals"]["received"] == billing["totals"]["receipts"], params
        listing = await client.get(
            org_url(d.org, "/payments"),
            headers=d.owner.headers,
            params={"date_from": "2026-09-01", "date_to": "2026-10-31", "limit": 100, **params},
        )
        assert listing.status_code == 200, listing.text
        rows = listing.json()["data"]
        live = [p for p in rows if p["state"] != "void"]
        void = [p for p in rows if p["state"] == "void"]
        totals = report["totals"]
        assert totals["payment_count"] == len(live), params
        assert totals["received"] == sum(p["amount"] for p in live), params
        assert totals["allocated"] == sum(p["allocated_total"] for p in live), params
        assert totals["on_account"] == sum(p["unallocated"] for p in live), params
        assert totals["void_count"] == len(void), params
        assert totals["void_amount"] == sum(p["amount"] for p in void), params


async def test_payments_need_report_financial(client: AsyncClient) -> None:
    d = await payment_dataset(client)
    for role in ("viewer", "staff"):
        response = await call(client, await staff(d.org, role), d.org, "payments", **AUTUMN)
        assert response.status_code == 403, role
        assert error(response)["code"] == "PERMISSION_DENIED"
    for role in ("manager", "billing", "partner", "admin"):
        response = await call(client, await staff(d.org, role), d.org, "payments", **AUTUMN)
        assert response.status_code == 200, role


async def test_another_practice_answers_404(client: AsyncClient) -> None:
    d = await payment_dataset(client)
    other_owner, other = await practice(client, "Other & Co")
    theirs = await make_client(other, "Theirs")
    assert (await call(client, other_owner, d.org, "payments", **AUTUMN)).status_code == 404
    for params in ({"client_id": theirs.id}, {"partner_user_id": other_owner.id}):
        response = await call(client, d.owner, d.org, "payments", **AUTUMN, **params)
        assert response.status_code == 404, params
        assert error(response)["code"] == "RESOURCE_NOT_FOUND"
    data = await get_data(client, other_owner, other, "payments", **AUTUMN)
    assert data["totals"]["received"] == 0  # nothing of the first practice leaks


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"group_by": "person"}, "query.group_by"),
        ({"method": "barter"}, "query.method"),
        ({"client_id": "nope"}, "query.client_id"),
        ({"date_from": "2026-09-30", "date_to": "2026-09-01"}, "date_from"),
        ({"date_from": "2010-01-01", "date_to": "2026-01-01"}, "date_to"),
        ({"format": "html"}, "query.format"),
    ],
)
async def test_payments_validation(client: AsyncClient, params: dict, field: str) -> None:
    owner, org = await practice(client)
    response = await call(client, owner, org, "payments", **params)
    assert response.status_code == 422, response.text
    body = error(response)
    assert body["code"] == "VALIDATION_ERROR"
    assert field in [d["field"] for d in body["details"]], body
