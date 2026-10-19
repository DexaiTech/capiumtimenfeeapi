"""The Client 360 overview: the figures behind the tabs of one client."""

from datetime import UTC, date, datetime, timedelta

from app.services import client_overview
from tests.billing_helpers import draft, hourly_job, issued, new_practice, pay
from tests.billing_helpers import org_url as billing_url
from tests.central_helpers import BAKERY, PLUMBING, enabled, new_team, send_event
from tests.clients_helpers import count_queries, create_client, org_url
from tests.domain_factories import (
    make_client,
    make_disbursement,
    make_job,
    make_time_entry,
    staff,
    vat_rate_id,
)
from tests.factories import add_member, create_user_with_org
from tests.reports_helpers import custom_role

TODAY = date.today()
SECTIONS = ("jobs", "time", "expenses", "wip", "invoices", "payments")


async def overview(client, p, actor=None, client_id=None, *, expect: int = 200) -> dict:
    response = await client.get(
        org_url(p.org, f"/clients/{client_id or p.client.id}/overview"),
        headers=(actor or p.owner).headers,
    )
    assert response.status_code == expect, response.text
    return response.json()["data"] if expect == 200 else response.json()["error"]


async def manual_invoice(
    client, p, net: int, *, issue_date: date, due_date: date, issue: bool = True, client_row=None
) -> dict:
    """An invoice with one manual line of ``net`` pence (20% VAT), issued on ``issue_date``."""
    row = client_row or p.client
    created = await draft(client, p.owner, p.org, row.id)
    line = await client.post(
        billing_url(p.org, f"/invoices/{created['id']}/lines"),
        headers=p.owner.headers,
        json={"description": "Advice", "unit_amount": net},
    )
    assert line.status_code == 201, line.text
    if not issue:
        return created
    return await issued(
        client,
        p.owner,
        p.org,
        created["id"],
        issue_date=issue_date.isoformat(),
        due_date=due_date.isoformat(),
    )


async def void_invoice(client, p, invoice_id: str) -> None:
    response = await client.post(
        billing_url(p.org, f"/invoices/{invoice_id}/void"),
        headers=p.owner.headers,
        json={"reason": "Raised in error"},
    )
    assert response.status_code == 200, response.text


async def listing(client, p, path: str, **params) -> list[dict]:
    response = await client.get(
        billing_url(p.org, path),
        headers=p.owner.headers,
        params={"client_id": str(p.client.id), "limit": 100, **params},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def build_world(client):
    """A practice whose first client has jobs, time, expenses, WIP, invoices and payments, with
    dates relative to today (so they classify the same way whichever day the tests run)."""
    p = await new_practice(client, vat_registered=True)
    zero = await vat_rate_id(p.org, "zero")
    hourly = await hourly_job(p, name="Bookkeeping")
    fixed = await make_job(p.org, p.client, "Tax return", billing_basis="fixed", fixed_fee=50_000)
    for name, status in (
        ("Planned", "planned"),
        ("On hold", "on_hold"),
        ("Completed", "completed"),
        ("Cancelled", "cancelled"),
    ):
        await make_job(p.org, p.client, name, status=status)
    await make_job(p.org, p.client, "Deleted", deleted_at=datetime.now(UTC))

    entries = {
        "recent": await make_time_entry(
            p.org,
            p.owner,
            client=p.client,
            job=hourly,
            minutes=60,
            entry_date=TODAY - timedelta(days=3),
        ),
        "fixed": await make_time_entry(
            p.org,
            p.owner,
            client=p.client,
            job=fixed,
            minutes=30,
            entry_date=TODAY - timedelta(days=35),
        ),
        "non_billable": await make_time_entry(
            p.org,
            p.owner,
            client=p.client,
            minutes=45,
            is_billable=False,
            entry_date=TODAY - timedelta(days=8),
        ),
        "last_year": await make_time_entry(
            p.org,
            p.owner,
            client=p.client,
            job=hourly,
            minutes=20,
            entry_date=date(TODAY.year - 1, 6, 15),
        ),
        "billed": await make_time_entry(
            p.org,
            p.owner,
            client=p.client,
            minutes=30,
            billing_status="billed",
            entry_date=TODAY - timedelta(days=20),
        ),
    }
    await make_disbursement(p.org, p.client, 1_300, incurred_on=TODAY - timedelta(days=2))
    await make_disbursement(p.org, p.client, 2_500, incurred_on=TODAY - timedelta(days=70))
    await make_disbursement(
        p.org, p.client, 1_000, incurred_on=TODAY - timedelta(days=1), vat_rate_id=zero
    )
    await make_disbursement(p.org, p.client, 700, is_billable=False)

    invoices = {
        "overdue": await manual_invoice(
            client,
            p,
            10_000,
            issue_date=TODAY - timedelta(days=60),
            due_date=TODAY - timedelta(days=30),
        ),
        "part_paid": await manual_invoice(
            client,
            p,
            10_000,
            issue_date=TODAY - timedelta(days=10),
            due_date=TODAY + timedelta(days=20),
        ),
        "issued": await manual_invoice(
            client,
            p,
            5_000,
            issue_date=TODAY - timedelta(days=5),
            due_date=TODAY + timedelta(days=25),
        ),
        "paid": await manual_invoice(
            client,
            p,
            2_000,
            issue_date=TODAY - timedelta(days=15),
            due_date=TODAY + timedelta(days=15),
        ),
        "old": await manual_invoice(
            client,
            p,
            3_000,
            issue_date=date(TODAY.year - 1, 12, 15),
            due_date=date(TODAY.year - 1, 12, 29),
        ),
        "void": await manual_invoice(
            client,
            p,
            1_000,
            issue_date=TODAY - timedelta(days=12),
            due_date=TODAY + timedelta(days=18),
        ),
        "draft": await manual_invoice(
            client, p, 4_000, issue_date=TODAY, due_date=TODAY, issue=False
        ),
    }
    await void_invoice(client, p, invoices["void"]["id"])
    payments = {
        "part": await pay(
            client,
            p.owner,
            p.org,
            p.client.id,
            5_000,
            received_on=TODAY - timedelta(days=8),
            allocations=[{"invoice_id": invoices["part_paid"]["id"], "amount": 5_000}],
        ),
        "full": await pay(
            client,
            p.owner,
            p.org,
            p.client.id,
            2_400,
            received_on=TODAY - timedelta(days=14),
            allocations=[{"invoice_id": invoices["paid"]["id"], "amount": 2_400}],
        ),
        "last_year": await pay(
            client, p.owner, p.org, p.client.id, 1_000, received_on=date(TODAY.year - 1, 12, 20)
        ),
        "voided": await pay(
            client, p.owner, p.org, p.client.id, 3_000, received_on=TODAY - timedelta(days=2)
        ),
    }
    for name, response in payments.items():
        assert response.status_code == 201, (name, response.text)
    voided = await client.post(
        billing_url(p.org, f"/payments/{payments['voided'].json()['data']['id']}/void"),
        headers=p.owner.headers,
        json={"reason": "Raised in error"},
    )
    assert voided.status_code == 200, voided.text
    return p, entries


# --- The figures ------------------------------------------------------------------------------------


async def test_an_empty_client_has_zero_everywhere(client):
    p = await new_practice(client)
    data = await overview(client, p)
    assert data["client_id"] == str(p.client.id)
    assert data["as_of"] == TODAY.isoformat()
    assert data["ytd_from"] == date(TODAY.year, 1, 1).isoformat()
    assert data["last_30d_from"] == (TODAY - timedelta(days=29)).isoformat()
    assert data["jobs"] == {
        "total": 0,
        "by_status": {
            "not_started": 0,
            "in_progress": 0,
            "on_hold": 0,
            "completed": 0,
            "closed": 0,
        },
    }
    assert data["time"] == {
        "scope": "all",
        "minutes_30d": 0,
        "billable_minutes_30d": 0,
        "minutes_ytd": 0,
        "billable_minutes_ytd": 0,
        "last_entry_on": None,
    }
    assert data["expenses"] == {
        "scope": "all",
        "count": 0,
        "net": 0,
        "gross": 0,
        "awaiting_approval": 0,
    }
    assert data["wip"] == {
        "time": 0,
        "fixed_fee": 0,
        "expense": 0,
        "total": 0,
        "ageing": {"0_30": 0, "31_60": 0, "61_90": 0, "90_plus": 0},
        "item_count": 0,
    }
    assert data["invoices"] == {
        "count": 0,
        "by_status": {
            "draft": 0,
            "issued": 0,
            "overdue": 0,
            "part_paid": 0,
            "paid": 0,
            "void": 0,
            "awaiting_approval": 0,
            "approved": 0,
            "credited": 0,
        },
        "billed_ytd": 0,
        "outstanding": 0,
        "overdue": 0,
        "last_invoice_on": None,
    }
    assert data["payments"] == {"received_ytd": 0, "count_ytd": 0, "last_payment_on": None}
    assert data["recent_activity"] == []
    assert data["tab_counts"] == {
        "jobs": 0,
        "time_entries": 0,
        "expenses": 0,
        "wip": 0,
        "invoices": 0,
        "payments": 0,
        "contacts": 0,
    }


async def test_the_overview_figures(client):
    p, entries = await build_world(client)
    data = await overview(client, p)

    # Jobs: a planned job counts as not started and a cancelled one as closed; deleted ones do not count.
    assert data["jobs"] == {
        "total": 6,
        "by_status": {
            "not_started": 1,
            "in_progress": 2,
            "on_hold": 1,
            "completed": 1,
            "closed": 1,
        },
    }

    # Time: 30 days up to and including today, and the year to date.
    last_30 = TODAY - timedelta(days=29)
    year = date(TODAY.year, 1, 1)
    all_entries = list(entries.values())
    in_30 = [e for e in all_entries if last_30 <= e.entry_date <= TODAY]
    in_ytd = [e for e in all_entries if year <= e.entry_date <= TODAY]
    assert data["time"] == {
        "scope": "all",
        "minutes_30d": sum(e.minutes for e in in_30),
        "billable_minutes_30d": sum(e.minutes for e in in_30 if e.is_billable),
        "minutes_ytd": sum(e.minutes for e in in_ytd),
        "billable_minutes_ytd": sum(e.minutes for e in in_ytd if e.is_billable),
        "last_entry_on": (TODAY - timedelta(days=3)).isoformat(),
    }
    assert data["time"]["minutes_30d"] == 60 + 45 + 30  # the 35 days old entry is outside
    assert data["time"]["billable_minutes_30d"] == 60 + 30

    # Expenses: every disbursement; VAT at the practice rate, none on the zero-rated one.
    assert data["expenses"] == {
        "scope": "all",
        "count": 4,
        "net": 1_300 + 2_500 + 1_000 + 700,
        "gross": 1_560 + 3_000 + 1_000 + 840,
        "awaiting_approval": 0,
    }

    # WIP: the same figures as the WIP screens.
    [row] = (await client.get(billing_url(p.org, "/wip"), headers=p.owner.headers)).json()["data"]
    assert data["wip"]["total"] == row["total"] == 12_000 + 6_000 + 4_000 + 1_300 + 2_500 + 1_000
    assert data["wip"]["time"] + data["wip"]["fixed_fee"] == row["time_value"] == 22_000
    assert data["wip"]["expense"] == row["disbursement_value"] == 4_800
    assert data["wip"]["fixed_fee"] == 6_000 and data["wip"]["time"] == 16_000
    assert data["wip"]["ageing"] == {
        "0_30": row["ages"]["days_0_30"],
        "31_60": row["ages"]["days_31_60"],
        "61_90": row["ages"]["days_61_90"],
        "90_plus": row["ages"]["days_91_plus"],
    }
    assert data["wip"]["ageing"] == {
        "0_30": 14_300,
        "31_60": 6_000,
        "61_90": 2_500,
        "90_plus": 4_000,
    }
    assert data["wip"]["item_count"] == row["item_count"] == 6
    items = (
        await client.get(billing_url(p.org, f"/clients/{p.client.id}/wip"), headers=p.owner.headers)
    ).json()["data"]
    assert items["totals"]["total"] == data["wip"]["total"]
    assert len(items["items"]) == data["wip"]["item_count"]

    # Invoices: the same figures as the invoice list.
    listed = await listing(client, p, "/invoices")
    by_status: dict[str, int] = {}
    for invoice in listed:
        by_status[invoice["display_status"]] = by_status.get(invoice["display_status"], 0) + 1
    assert data["invoices"]["by_status"] == {
        "draft": 1,
        "issued": 1,
        "overdue": 2,
        "part_paid": 1,
        "paid": 1,
        "void": 1,
        "awaiting_approval": 0,
        "approved": 0,
        "credited": 0,
    }
    assert {k: v for k, v in data["invoices"]["by_status"].items() if v} == by_status
    assert data["invoices"]["count"] == len(listed) == 7
    real = [i for i in listed if i["status"] == "issued"]
    assert data["invoices"]["outstanding"] == sum(i["balance"] for i in real)
    assert data["invoices"]["overdue"] == sum(
        i["balance"] for i in real if i["display_status"] == "overdue"
    )
    assert data["invoices"]["outstanding"] == 12_000 + 7_000 + 6_000 + 3_600
    assert data["invoices"]["overdue"] == 12_000 + 3_600
    assert data["invoices"]["billed_ytd"] == sum(
        i["net_total"] for i in real if year.isoformat() <= i["issue_date"] <= TODAY.isoformat()
    )
    assert data["invoices"]["last_invoice_on"] == max(i["issue_date"] for i in real)
    assert data["invoices"]["last_invoice_on"] == (TODAY - timedelta(days=5)).isoformat()
    # The same balances as the client's own financial snapshot.
    detail = (
        await client.get(org_url(p.org, f"/clients/{p.client.id}"), headers=p.owner.headers)
    ).json()["data"]
    assert detail["financials"]["outstanding_balance"] == data["invoices"]["outstanding"]
    assert detail["financials"]["overdue_balance"] == data["invoices"]["overdue"]
    assert detail["financials"]["wip_total"] == data["wip"]["total"]

    # Payments: received this year, voided ones left out.
    payments = await listing(client, p, "/payments")
    live = [x for x in payments if x["state"] != "void"]
    in_year = [x for x in live if year.isoformat() <= x["received_on"] <= TODAY.isoformat()]
    assert data["payments"] == {
        "received_ytd": sum(x["amount"] for x in in_year),
        "count_ytd": len(in_year),
        "last_payment_on": max(x["received_on"] for x in live),
    }
    assert data["payments"]["last_payment_on"] == (TODAY - timedelta(days=8)).isoformat()
    assert len(payments) == 4

    assert data["tab_counts"] == {
        "jobs": 6,
        "time_entries": 5,
        "expenses": 4,
        "wip": 6,
        "invoices": 7,
        "payments": 4,
        "contacts": 0,
    }


async def test_the_overview_has_no_costs_or_margins(client):
    p, *_ = await build_world(client)
    text = (
        await client.get(
            org_url(p.org, f"/clients/{p.client.id}/overview"), headers=p.owner.headers
        )
    ).text
    for word in ("cost", "margin", "profit"):
        assert word not in text.lower(), word


async def test_the_year_and_thirty_day_windows_have_exact_edges(client, monkeypatch):
    pinned = date(2026, 3, 15)
    monkeypatch.setattr(client_overview, "today_for", lambda organization: pinned)
    p = await new_practice(client, vat_registered=True)
    expectations = {
        date(2026, 3, 15): "today",
        date(2026, 2, 14): "29 days ago: the first day of the window",
        date(2026, 2, 13): "30 days ago: outside",
        date(2026, 1, 1): "the first day of the year",
        date(2025, 12, 31): "last year",
    }
    for day in expectations:
        await make_time_entry(p.org, p.owner, client=p.client, minutes=10, entry_date=day)
    await make_time_entry(
        p.org, p.owner, client=p.client, minutes=5, entry_date=date(2026, 3, 16)
    )  # tomorrow: in no window
    data = await overview(client, p)
    assert data["as_of"] == "2026-03-15" and data["ytd_from"] == "2026-01-01"
    assert data["last_30d_from"] == "2026-02-14"
    assert data["time"]["minutes_30d"] == 20  # 15 March and 14 February
    assert data["time"]["minutes_ytd"] == 40  # + 13 February and 1 January
    assert data["time"]["last_entry_on"] == "2026-03-16"
    assert data["tab_counts"]["time_entries"] == 6

    # An invoice due yesterday is overdue; one due today is not; payments and invoices on the
    # first day of the year count for the year, the day before does not.
    await manual_invoice(client, p, 10_000, issue_date=date(2026, 1, 1), due_date=date(2026, 3, 14))
    await manual_invoice(
        client, p, 5_000, issue_date=date(2025, 12, 31), due_date=date(2026, 3, 15)
    )
    data = await overview(client, p)
    assert (
        data["invoices"]["by_status"]["overdue"] == 1
        and data["invoices"]["by_status"]["issued"] == 1
    )
    assert (
        data["invoices"]["overdue"] == 12_000 and data["invoices"]["outstanding"] == 12_000 + 6_000
    )
    assert data["invoices"]["billed_ytd"] == 10_000
    assert data["invoices"]["last_invoice_on"] == "2026-01-01"
    for amount, day in (
        (1_000, date(2026, 1, 1)),
        (2_000, date(2025, 12, 31)),
        (4_000, date(2026, 3, 16)),
    ):
        assert (
            await pay(client, p.owner, p.org, p.client.id, amount, received_on=day)
        ).status_code == 201
    data = await overview(client, p)
    assert data["payments"] == {
        "received_ytd": 1_000,
        "count_ytd": 1,
        "last_payment_on": "2026-03-16",
    }


async def test_a_not_vat_registered_practice_has_no_vat_on_expenses(client):
    p = await new_practice(client, vat_registered=False)
    await make_disbursement(p.org, p.client, 1_300)
    data = await overview(client, p)
    assert data["expenses"]["net"] == data["expenses"]["gross"] == 1_300


async def test_a_client_default_vat_rate_applies_to_expenses_without_their_own(client):
    p = await new_practice(client, vat_registered=True)
    zero = await vat_rate_id(p.org, "zero")
    row = await make_client(p.org, "Zero Rated Ltd", default_vat_rate_id=zero)
    await make_disbursement(p.org, row, 1_000)
    data = await overview(client, p, client_id=row.id)
    assert (data["expenses"]["net"], data["expenses"]["gross"]) == (1_000, 1_000)
    plain = await overview(client, p)
    assert plain["expenses"]["count"] == 0


async def test_the_overview_only_counts_this_client(client):
    p, *_ = await build_world(client)
    other = await make_client(p.org, "Other Ltd")
    await make_time_entry(p.org, p.owner, client=other, minutes=999, entry_date=TODAY)
    await make_disbursement(p.org, other, 99_999)
    first = await overview(client, p)
    second = await overview(client, p, client_id=other.id)
    assert first["time"]["minutes_30d"] == 135
    assert second["time"]["minutes_30d"] == 999 and second["expenses"]["net"] == 99_999
    assert second["jobs"]["total"] == 0 and second["invoices"]["count"] == 0
    assert second["wip"]["total"] == 999 * 12_000 // 60 + 99_999


async def test_the_number_of_queries_does_not_grow_with_the_data(client):
    # The same practice settings and the same sections, a little data against a lot.
    small = await new_practice(client, vat_registered=True)
    row = await make_client(small.org, "Tiny Ltd")
    await make_time_entry(small.org, small.owner, client=row, entry_date=TODAY)
    await make_disbursement(small.org, row, 1_300)
    # One audit entry about the client, so the activity list is not empty either.
    edited = await client.patch(
        org_url(small.org, f"/clients/{row.id}"), headers=small.owner.headers, json={"notes": "x"}
    )
    assert edited.status_code == 200
    await overview(client, small, client_id=row.id)  # warm up
    with count_queries() as few:
        await overview(client, small, client_id=row.id)
    world, *_ = await build_world(client)
    await overview(client, world)
    with count_queries() as many:
        await overview(client, world)
    assert len(many) == len(few), (len(few), len(many))
    assert len(many) < 30


# --- Permissions ------------------------------------------------------------------------------------


async def test_every_section_the_caller_may_not_see_is_null(client):
    p, *_ = await build_world(client)
    sections_by_role = {
        "owner": set(SECTIONS),
        "manager": set(SECTIONS),
        "billing": set(SECTIONS),
        "viewer": set(SECTIONS),
        # job.read, time.log and disbursement.log: no WIP, invoices or payments.
        "staff": {"jobs", "time", "expenses"},
    }
    tab_of = {
        "jobs": "jobs",
        "time": "time_entries",
        "expenses": "expenses",
        "wip": "wip",
        "invoices": "invoices",
        "payments": "payments",
    }
    for role, present in sections_by_role.items():
        actor = p.owner if role == "owner" else await staff(p.org, role)
        data = await overview(client, p, actor)
        assert {s for s in SECTIONS if data[s] is not None} == present, role
        # The tab counts follow the sections, so a badge never shows what the tab would hide.
        assert {s for s, tab in tab_of.items() if data["tab_counts"][tab] is not None} == present, (
            role
        )
        assert data["tab_counts"]["contacts"] == 0


async def test_a_person_with_only_client_read_sees_no_section(client):
    p, *_ = await build_world(client)
    await custom_role(p.org, "Receptionist", {"organization.read", "client.read"})
    actor = await add_member(p.org["id"], "Receptionist")
    data = await overview(client, p, actor)
    assert {s: data[s] for s in SECTIONS} == dict.fromkeys(SECTIONS)
    assert data["tab_counts"] == {
        "jobs": None,
        "time_entries": None,
        "expenses": None,
        "wip": None,
        "invoices": None,
        "payments": None,
        "contacts": 0,
    }
    assert data["client_id"] == str(p.client.id) and data["as_of"] == TODAY.isoformat()


async def test_time_and_expenses_are_the_callers_own_without_the_read_all_permissions(client):
    p = await new_practice(client, vat_registered=True)
    worker = await staff(p.org, "staff")
    other = await staff(p.org, "staff")
    job = await hourly_job(p)
    await make_time_entry(p.org, worker, client=p.client, job=job, minutes=50, entry_date=TODAY)
    await make_time_entry(p.org, other, client=p.client, job=job, minutes=70, entry_date=TODAY)
    await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=90, entry_date=TODAY)
    await make_disbursement(p.org, p.client, 1_000, created_by=worker.id)
    await make_disbursement(p.org, p.client, 2_000, created_by=other.id)
    mine = await overview(client, p, worker)
    assert mine["time"]["scope"] == "own" and mine["time"]["minutes_30d"] == 50
    assert mine["expenses"] == {
        "scope": "own",
        "count": 1,
        "net": 1_000,
        "gross": 1_200,
        "awaiting_approval": 0,
    }
    assert (mine["tab_counts"]["time_entries"], mine["tab_counts"]["expenses"]) == (1, 1)
    assert mine["wip"] is None
    everyone = await overview(client, p)
    assert everyone["time"]["scope"] == "all" and everyone["time"]["minutes_30d"] == 210
    assert everyone["expenses"]["scope"] == "all" and everyone["expenses"]["count"] == 2
    # The viewer reads everyone's time (time.read_all) and every expense (wip.read).
    viewer = await staff(p.org, "viewer")
    seen = await overview(client, p, viewer)
    assert seen["time"]["scope"] == "all" and seen["expenses"]["scope"] == "all"


async def test_the_overview_needs_client_read_and_is_private_to_the_practice(client):
    p, *_ = await build_world(client)
    await custom_role(p.org, "Timekeeper", {"organization.read", "time.log"})
    blind = await add_member(p.org["id"], "Timekeeper")
    assert (await overview(client, p, blind, expect=403))["code"] == "PERMISSION_DENIED"
    other_owner, other_org = await create_user_with_org(client, "Second Practice")
    theirs = await create_client(client, other_owner, other_org, "Theirs Ltd")
    # Another practice's client is not found, whoever asks.
    assert (await overview(client, p, p.owner, theirs["id"], expect=404))[
        "code"
    ] == "RESOURCE_NOT_FOUND"
    response = await client.get(
        org_url(other_org, f"/clients/{p.client.id}/overview"), headers=other_owner.headers
    )
    assert response.status_code == 404
    outsider = await client.get(
        org_url(p.org, f"/clients/{p.client.id}/overview"), headers=other_owner.headers
    )
    assert outsider.status_code == 404
    missing = await overview(client, p, p.owner, "00000000-0000-4000-8000-000000000000", expect=404)
    assert missing["code"] == "RESOURCE_NOT_FOUND"
    assert (
        await client.get(org_url(p.org, "/clients/not-a-uuid/overview"), headers=p.owner.headers)
    ).status_code == 422
    assert (await client.get(org_url(p.org, f"/clients/{p.client.id}/overview"))).status_code == 401


# --- Recent activity --------------------------------------------------------------------------------


async def test_recent_activity_lists_the_last_ten_audit_events_about_the_client(client):
    p = await new_practice(client, vat_registered=True)
    created = await create_client(client, p.owner, p.org, "Active Ltd")
    url = org_url(p.org, f"/clients/{created['id']}")
    for n in range(12):
        response = await client.patch(url, headers=p.owner.headers, json={"notes": f"Note {n}"})
        assert response.status_code == 200
    data = await overview(client, p, client_id=created["id"])
    activity = data["recent_activity"]
    assert len(activity) == 10
    assert [e["action"] for e in activity] == ["client.updated"] * 10
    assert [e["at"] for e in activity] == sorted((e["at"] for e in activity), reverse=True)
    first = activity[0]
    assert set(first) == {
        "id",
        "action",
        "label",
        "at",
        "actor_user_id",
        "actor_name",
        "target_type",
        "target_id",
    }
    assert (first["label"], first["actor_user_id"], first["target_type"], first["target_id"]) == (
        "Client details updated",
        str(p.owner.id),
        "client",
        created["id"],
    )
    assert first["actor_name"] == p.owner.full_name


async def test_recent_activity_covers_contacts_jobs_invoices_and_payments_by_permission(client):
    p = await new_practice(client, vat_registered=True)
    job = await hourly_job(p)
    entry = await make_time_entry(p.org, p.owner, client=p.client, job=job, entry_date=TODAY)
    contact = await client.post(
        org_url(p.org, f"/clients/{p.client.id}/contacts"),
        headers=p.owner.headers,
        json={"name": "Pat Primary"},
    )
    assert contact.status_code == 201
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    await issued(client, p.owner, p.org, invoice["id"], issue_date=TODAY.isoformat())
    paid = await pay(client, p.owner, p.org, p.client.id, 5_000)
    assert paid.status_code == 201

    everything = await overview(client, p)
    actions = [e["action"] for e in everything["recent_activity"]]
    assert actions == [
        "payment.recorded",
        "invoice.issued",
        "invoice.draft_created",
        "client_contact.created",
    ]
    labels = [e["label"] for e in everything["recent_activity"]]
    assert labels == [
        "Payment recorded",
        "Invoice issued",
        "Draft invoice created",
        "Contact added",
    ]
    # Without invoice.read and payment.read only the client's own events are visible.
    staff_member = await staff(p.org, "staff")
    seen = await overview(client, p, staff_member)
    assert [e["action"] for e in seen["recent_activity"]] == ["client_contact.created"]
    billing = await staff(p.org, "billing")
    assert [e["action"] for e in (await overview(client, p, billing))["recent_activity"]] == actions


async def test_system_events_have_no_actor_and_a_merged_clients_events_belong_to_the_survivor(
    client,
):
    team = await new_team(client)
    source = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    target = (await enabled(client, team.owner, team.org, PLUMBING))["client"]
    await send_event(
        client, team.owner, team.org, "renamed", BAKERY, name="Northgate Bakehouse Ltd"
    )
    await send_event(client, team.owner, team.org, "merged", BAKERY, into_central_id=PLUMBING)
    survivor = await overview(client, team, client_id=target["id"])
    entries = {e["action"]: e for e in survivor["recent_activity"]}
    assert set(entries) == {
        "client.central_enabled",
        "client.central_merged",
        "client.central_renamed",
    }
    merged = entries["client.central_merged"]
    assert merged["actor_user_id"] is None and merged["actor_name"] is None
    assert merged["label"] == "Merged with another client" and merged["target_id"] == target["id"]
    # The merged-away client's own history is on the survivor too.
    assert entries["client.central_renamed"]["target_id"] == source["id"]
    assert entries["client.central_renamed"]["label"] == "Renamed by the central client service"
    assert entries["client.central_renamed"]["actor_user_id"] is None


async def test_the_overview_is_the_same_whoever_the_caller_for_what_they_may_see(client):
    p, *_ = await build_world(client)
    owner_view = await overview(client, p)
    manager_view = await overview(client, p, await staff(p.org, "manager"))
    for section in ("jobs", "wip", "invoices", "payments", "expenses"):
        assert owner_view[section] == manager_view[section], section
