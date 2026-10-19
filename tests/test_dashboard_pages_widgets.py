"""The dashboard widgets: My Timesheet, Pending Approvals, Missing Timesheets, Top Clients by WIP,
Billing Trend and Debtor Ageing; and that their figures agree with the screens they summarise."""

from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import event

from app.db.models import Timesheet
from app.db.session import get_engine
from tests.dashboard_page_helpers import (
    START,
    Cast,
    Money,
    cards_by_key,
    get_page,
    page_url,
    pin_clock,
    seed_approvals,
    seed_money,
    seed_time,
    team,
)
from tests.domain_factories import make_client, make_time_entry, make_timesheet, practice
from tests.factories import API, add_member
from tests.reports_helpers import custom_role, make_timer, report, set_member, set_org, update_row


async def _world(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> tuple[Cast, Money]:
    pin_clock(monkeypatch)
    c = await team(client)
    alpha = await seed_time(c)
    money = await seed_money(c, alpha)
    await seed_approvals(c)
    return c, money


# --- My Timesheet ---------------------------------------------------------------------------


async def test_my_timesheet(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = await _world(client, monkeypatch)
    await make_timer(
        c.org,
        c.owner,
        started_at=datetime(2026, 10, 16, 23, 0, tzinfo=UTC),
        accumulated_seconds=600,
        description="Review",
    )
    data = await get_page(client, c.owner, c.org, "my-timesheet")

    assert data["visible"] is True
    assert (data["week_start"], data["week_end"], data["as_at"]) == (
        "2026-10-12",
        "2026-10-18",
        "2026-10-17",
    )
    assert (data["timesheet_status"], data["decision_note"]) == ("open", None)
    assert data["timesheet_id"] is not None
    assert (data["total_minutes"], data["chargeable_minutes"], data["non_chargeable_minutes"]) == (
        120,
        90,
        30,
    )
    # 5 working days at 7h30; today, Saturday, is a day off.
    assert (data["target_minutes"], data["target_to_date_minutes"]) == (2_250, 2_250)
    assert (data["today_minutes"], data["today_target_minutes"]) == (0, 0)
    assert (data["utilisation_bp"], data["chargeable_target_bp"]) == (400, 7_500)
    assert [
        (d["date"], d["minutes"], d["billable_minutes"], d["target_minutes"]) for d in data["days"]
    ] == [
        ("2026-10-12", 90, 90, 450),
        ("2026-10-13", 30, 0, 450),
        ("2026-10-14", 0, 0, 450),
        ("2026-10-15", 0, 0, 450),
        ("2026-10-16", 0, 0, 450),
        ("2026-10-17", 0, 0, 0),
        ("2026-10-18", 0, 0, 0),
    ]
    assert [(d["is_working_day"], d["is_missing"]) for d in data["days"]] == [
        (True, False),
        (True, False),
        (True, True),
        (True, True),
        (True, True),
        (False, False),
        (False, False),
    ]
    assert data["missing_days"] == ["2026-10-14", "2026-10-15", "2026-10-16"]
    assert data["entry_count"] == 2
    newest, oldest = data["entries"]
    assert (newest["entry_date"], newest["minutes"], newest["is_billable"]) == (
        "2026-10-13",
        30,
        False,
    )
    assert (newest["client_name"], newest["job_number"]) == (None, None)
    assert (oldest["entry_date"], oldest["minutes"], oldest["is_billable"]) == (
        "2026-10-12",
        90,
        True,
    )
    assert (oldest["client_name"], oldest["job_number"], oldest["job_name"]) == (
        "Alpha Ltd",
        "JT0001",
        "Accounts",
    )
    assert oldest["billing_status"] == "unbilled"
    timer = data["timer"]
    assert (timer["is_running"], timer["accumulated_seconds"], timer["elapsed_seconds"]) == (
        True,
        600,
        2_400,
    )
    assert timer["description"] == "Review"


async def test_a_returned_timesheet_shows_why(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    first = await get_page(client, c.owner, c.org, "my-timesheet")
    await update_row(
        Timesheet, first["timesheet_id"], status="rejected", decision_note="Wrong job on Monday"
    )
    data = await get_page(client, c.owner, c.org, "my-timesheet")
    assert (data["timesheet_status"], data["decision_note"]) == ("rejected", "Wrong job on Monday")


async def test_an_empty_week_and_other_roles(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    data = await get_page(client, c.olly, c.org, "my-timesheet")
    assert data["visible"] is True
    assert (data["timesheet_id"], data["timesheet_status"]) == (None, "open")
    assert (data["total_minutes"], data["entry_count"], data["entries"], data["timer"]) == (
        0,
        0,
        [],
        None,
    )
    assert data["utilisation_bp"] == 0
    assert len(data["days"]) == 7

    viewer = await add_member(c.org["id"], "viewer")  # cannot record time
    hidden = await get_page(client, viewer, c.org, "my-timesheet")
    assert hidden["visible"] is False
    assert (hidden["total_minutes"], hidden["days"], hidden["entries"], hidden["timer"]) == (
        0,
        [],
        [],
        None,
    )
    assert hidden["as_at"] == "2026-10-17"


async def test_only_the_latest_fifty_entries_are_listed(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    for minute in range(1, 52):
        await make_time_entry(
            c.org, c.sam, entry_date=date(2026, 10, 14), minutes=minute, timesheet_status="approved"
        )
    data = await get_page(client, c.sam, c.org, "my-timesheet")
    # The 51 new entries and the 30 minutes on Gamma of 13 October.
    assert data["entry_count"] == 52
    assert len(data["entries"]) == 50
    assert data["total_minutes"] == 30 + sum(range(1, 52))


# --- Pending approvals ----------------------------------------------------------------------


async def test_pending_approvals_in_one_queue(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    await make_time_entry(
        c.org, c.sam, entry_date=date(2026, 10, 6), minutes=150, timesheet_status="submitted"
    )
    data = await get_page(client, c.owner, c.org, "pending-approvals")
    assert (data["visible"], data["total"]) == (True, 2)
    assert data["kinds"] == [
        {"kind": "timesheet", "count": 1, "oldest_date": "2026-10-05", "link": "/time/approvals"},
        {
            "kind": "expense",
            "count": 1,
            "oldest_date": "2026-09-10",
            "link": "/expenses/approvals",
        },
    ]
    claim, sheet = data["items"]  # oldest first
    assert (claim["kind"], claim["title"], claim["subject"]) == (
        "expense",
        "Olly Other",
        "Train to Leeds",
    )
    assert (claim["date"], claim["amount"], claim["minutes"]) == ("2026-09-10", 1_200, None)
    assert claim["link"] == "/expenses/approvals"
    assert (sheet["kind"], sheet["title"], sheet["subject"]) == ("timesheet", "Sam Staff", None)
    assert (sheet["date"], sheet["minutes"], sheet["amount"]) == ("2026-10-05", 150, None)
    assert sheet["link"] == "/time/approvals"

    only = await get_page(client, c.owner, c.org, "pending-approvals", kind="timesheet")
    assert [item["kind"] for item in only["items"]] == ["timesheet"]
    assert only["total"] == 2  # the counts stay whole
    one = await get_page(client, c.owner, c.org, "pending-approvals", limit=1)
    assert [item["kind"] for item in one["items"]] == ["expense"]
    absent = await get_page(client, c.owner, c.org, "pending-approvals", kind="invoice")
    assert (absent["items"], absent["visible"]) == ([], True)
    bad = await get_page(client, c.owner, c.org, "pending-approvals", status=422, kind="salary")
    assert bad["code"] == "VALIDATION_ERROR"


async def test_an_approver_decides_on_the_people_they_manage(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    await custom_role(c.org, "Lead", {"timesheet.approve", "expense.approve"})
    lead = await add_member(c.org["id"], "Lead", full_name="Lee Lead")
    await set_member(c.org, c.sam, manager_user_id=lead.id)
    data = await get_page(client, lead, c.org, "pending-approvals")
    assert [(k["kind"], k["count"]) for k in data["kinds"]] == [("timesheet", 1), ("expense", 0)]
    assert [(i["kind"], i["title"]) for i in data["items"]] == [("timesheet", "Sam Staff")]
    assert data["total"] == 1

    nobody = await add_member(c.org["id"], "Lead", full_name="Nobody Manages")
    empty = await get_page(client, nobody, c.org, "pending-approvals")
    assert (empty["visible"], empty["total"], empty["items"]) == (True, 0, [])


async def test_one_kind_of_approval_only(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    await custom_role(c.org, "Expenses", {"expense.approve", "expense.read_all"})
    person = await add_member(c.org["id"], "Expenses")
    data = await get_page(client, person, c.org, "pending-approvals")
    assert [(k["kind"], k["count"]) for k in data["kinds"]] == [("expense", 1)]
    # Timesheets are not theirs to decide: asking for them lists nothing.
    asked = await get_page(client, person, c.org, "pending-approvals", kind="timesheet")
    assert asked["items"] == []


async def test_pending_approvals_are_hidden_without_an_approve_permission(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    for role in ("staff", "viewer", "billing"):
        person = await add_member(c.org["id"], role)
        data = await get_page(client, person, c.org, "pending-approvals")
        assert (data["visible"], data["total"], data["kinds"], data["items"]) == (
            False,
            0,
            [],
            [],
        ), role


async def test_nobody_decides_on_their_own_submission(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    await make_timesheet(c.org, c.mia, date(2026, 9, 28), "submitted")
    mia = await get_page(client, c.mia, c.org, "pending-approvals", kind="timesheet")
    assert [i["title"] for i in mia["items"]] == ["Sam Staff"]  # Mia's own is left out
    await make_timesheet(c.org, c.owner, date(2026, 9, 28), "submitted")
    owner = await get_page(client, c.owner, c.org, "pending-approvals", kind="timesheet")
    assert len(owner["items"]) == 3  # the owner holds organization.update


# --- Missing timesheets ---------------------------------------------------------------------


async def _listed(client: AsyncClient, actor, org: dict, **params: str) -> list[dict]:
    response = await client.get(
        f"{API}/organizations/{org['id']}/timesheets/missing", headers=actor.headers, params=params
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_missing_timesheets_of_the_last_completed_week(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    data = await get_page(client, c.owner, c.org, "missing-timesheets")
    assert data["visible"] is True
    assert data["scope"] == "practice"
    assert (data["week_start"], data["week_end"]) == ("2026-10-05", "2026-10-11")
    assert data["last_completed_week_start"] == "2026-10-05"
    assert data["count"] == 2
    assert {(r["user_id"], r["status"]) for r in data["rows"]} == {
        (str(c.owner.id), "open"),
        (str(c.olly.id), "none"),
    }
    assert data["can_remind"] is True
    # The very rows of GET /timesheets/missing.
    assert data["rows"] == await _listed(client, c.owner, c.org)

    earlier = await get_page(client, c.owner, c.org, "missing-timesheets", week_start="2026-09-30")
    assert (earlier["week_start"], earlier["week_end"]) == ("2026-09-28", "2026-10-04")
    assert [(r["user_id"], r["status"]) for r in earlier["rows"]] == [(str(c.olly.id), "rejected")]
    assert earlier["rows"] == await _listed(client, c.owner, c.org, week_start="2026-09-30")
    none = await get_page(client, c.owner, c.org, "missing-timesheets", week_start="2026-09-14")
    assert (none["count"], none["rows"]) == (0, [])
    bad = await get_page(
        client, c.owner, c.org, "missing-timesheets", status=422, week_start="1800-01-01"
    )
    assert bad["code"] == "VALIDATION_ERROR"


async def test_missing_timesheets_follow_the_callers_scope(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    await custom_role(c.org, "Lead", {"timesheet.approve"})
    lead = await add_member(c.org["id"], "Lead", full_name="Lee Lead")
    await set_member(c.org, lead, start_date=START)
    await set_member(c.org, c.olly, manager_user_id=lead.id)
    data = await get_page(client, lead, c.org, "missing-timesheets")
    # Their team (Olly) and themselves; the owner and the rest are out of sight.
    assert data["scope"] == "team"
    assert {r["user_id"] for r in data["rows"]} == {str(c.olly.id), str(lead.id)}
    assert data["can_remind"] is True

    reader = await add_member(c.org["id"], "viewer")  # time.read_all, cannot approve
    seen = await get_page(client, reader, c.org, "missing-timesheets")
    # The owner, Olly, Lee and the viewer themselves (no start date: expected every week).
    assert (seen["scope"], seen["count"], seen["can_remind"]) == ("practice", 4, False)

    staffer = await add_member(c.org["id"], "staff")
    hidden = await get_page(client, staffer, c.org, "missing-timesheets")
    assert (hidden["visible"], hidden["scope"], hidden["count"], hidden["rows"]) == (
        False,
        None,
        0,
        [],
    )
    assert (hidden["can_remind"], hidden["week_start"]) == (False, "2026-10-05")


async def test_the_missing_timesheets_card_agrees_with_the_week_lists(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    cards = cards_by_key(await get_page(client, c.owner, c.org, "cards", period="ytd"))
    rows: list[dict] = []
    week = date(2025, 12, 29)  # the week of 1 January
    while week <= date(2026, 10, 5):
        rows += await _listed(client, c.owner, c.org, week_start=week.isoformat())
        week += timedelta(days=7)
    assert cards["missing_timesheets"]["value"] == len(rows) == 4
    assert (
        cards["missing_timesheets"]["secondary"]["value"] == len({r["user_id"] for r in rows}) == 2
    )


# --- Top clients by WIP ---------------------------------------------------------------------


async def test_top_clients_by_wip(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    c, m = await _world(client, monkeypatch)
    data = await get_page(client, c.owner, c.org, "top-clients-by-wip", period="this_month")
    assert (data["visible"], data["period"], data["date_from"]) == (
        True,
        "this_month",
        "2026-10-01",
    )
    assert (data["total"], data["previous_total"], data["client_count"], data["others_amount"]) == (
        22_300,
        12_000,
        3,
        0,
    )
    alpha, gamma, beta = data["items"]
    assert alpha == {
        "client_id": str(m.alpha.id),
        "client_code": "ALP",
        "client_name": "Alpha Ltd",
        "amount": 15_000,
        "time_value": 15_000,
        "disbursement_value": 0,
        "minutes": 75,
        "oldest_date": "2026-07-01",
        "over_90_days": 12_000,
        "share_bp": 6_726,
        "previous_amount": 12_000,
        "change": 3_000,
    }
    assert (gamma["client_name"], gamma["amount"], gamma["share_bp"], gamma["change"]) == (
        "Gamma plc",
        6_000,
        2_691,
        6_000,
    )
    assert (beta["client_name"], beta["amount"], beta["disbursement_value"], beta["minutes"]) == (
        "Beta LLP",
        1_300,
        1_300,
        0,
    )
    assert (beta["oldest_date"], beta["share_bp"], beta["previous_amount"]) == (
        "2026-10-01",
        583,
        0,
    )

    week = await get_page(client, c.owner, c.org, "top-clients-by-wip", period="this_week")
    assert week["previous_total"] == 16_300
    assert {i["client_name"]: (i["previous_amount"], i["change"]) for i in week["items"]} == {
        "Alpha Ltd": (15_000, 0),
        "Gamma plc": (0, 6_000),
        "Beta LLP": (1_300, 0),
    }

    top = await get_page(client, c.owner, c.org, "top-clients-by-wip", limit=1)
    assert [i["client_name"] for i in top["items"]] == ["Alpha Ltd"]
    assert (top["total"], top["client_count"], top["others_amount"]) == (22_300, 3, 7_300)


async def test_top_clients_by_wip_agree_with_the_wip_report(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    wip = await report(client, c.owner, c.org, "wip")
    widget = await get_page(client, c.owner, c.org, "top-clients-by-wip")
    assert widget["total"] == wip["totals"]["total"]
    per_client: dict[str, int] = {}
    for row in wip["rows"]:
        per_client[row["client_id"]] = per_client.get(row["client_id"], 0) + row["total"]
    assert {i["client_id"]: i["amount"] for i in widget["items"]} == per_client


async def test_wip_follows_the_timesheet_approval_setting(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, m = await _world(client, monkeypatch)
    before = await get_page(client, c.owner, c.org, "top-clients-by-wip")
    assert before["total"] == 22_300
    # Without approval the owner's 90 billable minutes on an open timesheet (18 000) count too.
    await set_org(c.org, require_timesheet_approval=False)
    after = await get_page(client, c.owner, c.org, "top-clients-by-wip")
    assert after["total"] == 40_300
    assert {i["client_id"]: i["amount"] for i in after["items"]}[str(m.alpha.id)] == 33_000


async def test_top_clients_by_wip_visibility(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    viewer = await add_member(c.org["id"], "viewer")  # wip.read
    assert (await get_page(client, viewer, c.org, "top-clients-by-wip"))["total"] == 22_300
    staffer = await add_member(c.org["id"], "staff")
    hidden = await get_page(client, staffer, c.org, "top-clients-by-wip")
    assert (hidden["visible"], hidden["total"], hidden["items"], hidden["client_count"]) == (
        False,
        0,
        [],
        0,
    )
    assert hidden["date_from"] == "2026-10-01"
    error = await get_page(client, c.owner, c.org, "top-clients-by-wip", status=422, limit=0)
    assert error["code"] == "VALIDATION_ERROR"
    other, other_org = await practice(client, "Other & Co")
    assert (await get_page(client, other, other_org, "top-clients-by-wip"))["items"] == []
    assert (await get_page(client, other, c.org, "top-clients-by-wip", status=404))[
        "code"
    ] == "RESOURCE_NOT_FOUND"


# --- Billing trend --------------------------------------------------------------------------


async def test_billing_trend(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = await _world(client, monkeypatch)
    data = await get_page(client, c.owner, c.org, "billing-trend")
    assert (data["visible"], data["months"], len(data["series"])) == (True, 12, 12)
    series = {row["month"]: row for row in data["series"]}
    assert data["series"][0]["month"] == "2025-11-01"
    assert data["series"][-1]["month"] == "2026-10-01"
    assert [(m, r["billed"], r["invoice_count"]) for m, r in series.items() if r["billed"]] == [
        ("2026-06-01", 100, 1),
        ("2026-08-01", 2_000, 1),
        ("2026-09-01", 500, 1),
        ("2026-10-01", 1_500, 2),
    ]
    october = series["2026-10-01"]
    assert (october["credit_notes"], october["credit_note_count"], october["net_billed"]) == (
        200,
        1,
        1_300,
    )
    assert october["receipts"] == 1_000
    assert series["2026-09-01"]["receipts"] == 400
    assert sum(r["receipts"] for r in data["series"]) == 1_400
    assert series["2026-07-01"] == {
        "month": "2026-07-01",
        "billed": 0,
        "invoice_count": 0,
        "credit_notes": 0,
        "credit_note_count": 0,
        "net_billed": 0,
        "receipts": 0,
    }
    assert data["this_month"] == {
        "date_from": "2026-10-01",
        "date_to": "2026-10-17",
        "billed": 1_500,
        "credit_notes": 200,
        "net_billed": 1_300,
        "receipts": 1_000,
    }
    # 1-17 September: only invoice B (the payment of the 30th is later).
    assert data["last_month"] == {
        "date_from": "2026-09-01",
        "date_to": "2026-09-17",
        "billed": 500,
        "credit_notes": 0,
        "net_billed": 500,
        "receipts": 0,
    }
    assert data["billed_comparison"] == {
        "basis": "previous_period",
        "previous": 500,
        "change": 1_000,
        "change_bp": 20_000,
    }

    one = await get_page(client, c.owner, c.org, "billing-trend", months=1)
    assert [r["month"] for r in one["series"]] == ["2026-10-01"]
    for bad in (0, 25):
        error = await get_page(client, c.owner, c.org, "billing-trend", status=422, months=bad)
        assert error["code"] == "VALIDATION_ERROR"


async def test_the_billing_trend_agrees_with_the_billing_report_and_the_card(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    widget = await get_page(client, c.owner, c.org, "billing-trend")
    summary = await report(
        client,
        c.owner,
        c.org,
        "billing-summary",
        date_from="2025-11-01",
        date_to="2026-10-17",
        group_by="month",
    )
    by_month = {row["key"]: row for row in summary["rows"]}
    for row in widget["series"]:
        found = by_month.get(row["month"][:7])
        expected = (row["billed"], row["credit_notes"], row["net_billed"], row["receipts"])
        if found is None:
            assert expected == (0, 0, 0, 0), row["month"]
        else:
            assert expected == (
                found["invoiced_net"],
                found["credited_net"],
                found["net_billed"],
                found["receipts"],
            ), row["month"]
    card = cards_by_key(await get_page(client, c.owner, c.org, "cards", period="this_month"))[
        "billed"
    ]
    assert (card["value"], card["comparison"]) == (
        widget["this_month"]["billed"],
        widget["billed_comparison"],
    )


async def test_billing_trend_visibility_and_isolation(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    staffer = await add_member(c.org["id"], "staff")
    hidden = await get_page(client, staffer, c.org, "billing-trend")
    assert (hidden["visible"], hidden["series"]) == (False, [])
    assert hidden["this_month"]["billed"] == 0
    for role in ("viewer", "billing"):
        person = await add_member(c.org["id"], role)
        assert (await get_page(client, person, c.org, "billing-trend"))["visible"] is True
    other, other_org = await practice(client, "Other & Co")
    empty = await get_page(client, other, other_org, "billing-trend", months=3)
    assert [r["billed"] for r in empty["series"]] == [0, 0, 0]
    assert empty["billed_comparison"]["change_bp"] is None
    assert (await get_page(client, other, c.org, "billing-trend", status=404))[
        "code"
    ] == "RESOURCE_NOT_FOUND"


async def test_january_reaches_back_across_the_year(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin_clock(monkeypatch, datetime(2027, 1, 20, 12, 0, tzinfo=UTC))
    owner, org = await practice(client)
    data = await get_page(client, owner, org, "billing-trend", months=3)
    assert [r["month"] for r in data["series"]] == ["2026-11-01", "2026-12-01", "2027-01-01"]
    assert (data["last_month"]["date_from"], data["last_month"]["date_to"]) == (
        "2026-12-01",
        "2026-12-20",
    )


# --- Debtor ageing --------------------------------------------------------------------------


async def test_debtor_ageing(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    c, m = await _world(client, monkeypatch)
    data = await get_page(client, c.owner, c.org, "debtor-ageing", period="this_month")
    assert (data["visible"], data["period"], data["date_from"]) == (
        True,
        "this_month",
        "2026-10-01",
    )
    assert (data["total"], data["overdue_total"]) == (4_280, 3_080)
    assert (data["invoice_count"], data["debtor_count"]) == (5, 3)
    # As at 30 September: B 600, C 2 000 (the credit note is later) and D 120.
    assert (data["previous_total"], data["previous_overdue_total"]) == (2_720, 2_120)
    assert data["buckets"] == [
        {"key": "current", "amount": 1_200, "invoice_count": 2, "share_bp": 2_804},
        {"key": "days_1_30", "amount": 1_200, "invoice_count": 1, "share_bp": 2_804},
        {"key": "days_31_60", "amount": 1_760, "invoice_count": 1, "share_bp": 4_112},
        {"key": "days_61_90", "amount": 0, "invoice_count": 0, "share_bp": 0},
        {"key": "days_90_plus", "amount": 120, "invoice_count": 1, "share_bp": 280},
    ]
    alpha, gamma, beta = data["top_debtors"]
    assert alpha == {
        "client_id": str(m.alpha.id),
        "client_code": "ALP",
        "client_name": "Alpha Ltd",
        "invoice_count": 3,
        "total": 3_560,
        "overdue": 2_960,
        "oldest_due_date": "2026-08-31",
        "current": 600,
        "days_1_30": 1_200,
        "days_31_60": 1_760,
        "days_61_90": 0,
        "days_90_plus": 0,
    }
    assert (gamma["client_name"], gamma["total"], gamma["overdue"], gamma["days_90_plus"]) == (
        "Gamma plc",
        120,
        120,
        120,
    )
    assert gamma["oldest_due_date"] == "2026-06-30"
    # Due today is not overdue.
    assert (beta["client_name"], beta["total"], beta["overdue"], beta["oldest_due_date"]) == (
        "Beta LLP",
        600,
        0,
        None,
    )

    week = await get_page(client, c.owner, c.org, "debtor-ageing", period="this_week")
    assert (week["previous_total"], week["previous_overdue_total"]) == (4_280, 1_880)
    two = await get_page(client, c.owner, c.org, "debtor-ageing", limit=2)
    assert [d["client_name"] for d in two["top_debtors"]] == ["Alpha Ltd", "Gamma plc"]
    assert (two["total"], two["debtor_count"]) == (4_280, 3)


async def test_debtor_ageing_agrees_with_the_aged_debtors_report_and_the_card(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    aged = await report(client, c.owner, c.org, "aged-debtors")
    widget = await get_page(client, c.owner, c.org, "debtor-ageing")
    totals = aged["totals"]
    assert {b["key"]: b["amount"] for b in widget["buckets"]} == {
        "current": totals["current"],
        "days_1_30": totals["days_1_30"],
        "days_31_60": totals["days_31_60"],
        "days_61_90": totals["days_61_90"],
        "days_90_plus": totals["days_91_plus"],
    }
    assert (widget["total"], widget["invoice_count"]) == (totals["total"], totals["invoice_count"])
    card = cards_by_key(await get_page(client, c.owner, c.org, "cards"))["overdue_debtors"]
    assert (card["value"], card["secondary"]["value"]) == (widget["overdue_total"], widget["total"])


async def test_debtor_ageing_visibility_and_an_empty_practice(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    staffer = await add_member(c.org["id"], "staff")
    hidden = await get_page(client, staffer, c.org, "debtor-ageing")
    assert (hidden["visible"], hidden["total"], hidden["buckets"], hidden["top_debtors"]) == (
        False,
        0,
        [],
        [],
    )
    for role in ("viewer", "billing"):
        person = await add_member(c.org["id"], role)
        assert (await get_page(client, person, c.org, "debtor-ageing"))["total"] == 4_280
    other, other_org = await practice(client, "Other & Co")
    empty = await get_page(client, other, other_org, "debtor-ageing")
    assert (empty["visible"], empty["total"], empty["invoice_count"], empty["debtor_count"]) == (
        True,
        0,
        0,
        0,
    )
    assert [(b["key"], b["amount"], b["share_bp"]) for b in empty["buckets"]] == [
        ("current", 0, None),
        ("days_1_30", 0, None),
        ("days_31_60", 0, None),
        ("days_61_90", 0, None),
        ("days_90_plus", 0, None),
    ]
    assert empty["top_debtors"] == []
    assert (await get_page(client, other, c.org, "debtor-ageing", status=404))[
        "code"
    ] == "RESOURCE_NOT_FOUND"


# --- Every widget loads on its own, in a fixed number of queries -----------------------------

WIDGETS = (
    "cards",
    "my-actions",
    "my-timesheet",
    "pending-approvals",
    "missing-timesheets",
    "top-clients-by-wip",
    "billing-trend",
    "debtor-ageing",
)


async def test_every_widget_answers_on_its_own_with_a_stable_number_of_queries(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    statements: list[str] = []

    def count(conn, cursor, statement, *args) -> None:
        statements.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        first: dict[str, int] = {}
        for widget in WIDGETS:
            statements.clear()
            await get_page(client, c.owner, c.org, widget)
            first[widget] = len(statements)
        extra = await make_client(c.org, "More Ltd")
        for day in range(1, 8):
            await make_time_entry(
                c.org,
                c.sam,
                client=extra,
                entry_date=date(2026, 9, day),
                timesheet_status="approved",
            )
        for widget in WIDGETS:
            statements.clear()
            await get_page(client, c.owner, c.org, widget)
            assert len(statements) == first[widget], widget
            assert first[widget] <= 45, (widget, first[widget])
    finally:
        event.remove(engine, "before_cursor_execute", count)


async def test_the_widgets_do_not_need_each_other(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    for widget in WIDGETS:
        response = await client.get(page_url(c.org, widget), headers=c.sam.headers)
        assert response.status_code == 200, (widget, response.text)
        assert response.json()["success"] is True
