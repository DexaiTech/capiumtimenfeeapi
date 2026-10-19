"""The dashboard cards: figures, comparisons and visibility for each period."""

from datetime import date, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import event

from app.db.session import get_engine
from tests.dashboard_page_helpers import (
    START,
    TODAY,
    Cast,
    Money,
    cards_by_key,
    get_page,
    pin_clock,
    seed_approvals,
    seed_money,
    seed_time,
    team,
)
from tests.domain_factories import make_client, make_time_entry, make_timesheet, practice
from tests.factories import add_member
from tests.reports_helpers import custom_role, set_member, set_org

ORDER = [
    "my_hours",
    "missing_timesheets",
    "pending_approvals",
    "unbilled_wip",
    "billed",
    "overdue_debtors",
]


async def _world(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> tuple[Cast, Money]:
    pin_clock(monkeypatch)
    c = await team(client)
    alpha = await seed_time(c)
    money = await seed_money(c, alpha)
    await seed_approvals(c)
    return c, money


def _working_days(start: date, end: date) -> int:
    return sum(1 for n in range((end - start).days + 1) if (start + timedelta(n)).weekday() < 5)


async def test_cards_this_week(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = await _world(client, monkeypatch)
    data = await get_page(client, c.owner, c.org, "cards", period="this_week")

    assert data["period"] == "this_week"
    assert (data["date_from"], data["date_to"]) == ("2026-10-12", "2026-10-17")
    assert (data["previous_from"], data["previous_to"]) == ("2026-10-05", "2026-10-10")
    assert data["as_at"] == "2026-10-17"
    assert [card["key"] for card in data["cards"]] == ORDER
    assert all(card["visible"] for card in data["cards"])
    cards = cards_by_key(data)

    hours = cards["my_hours"]
    assert (hours["title"], hours["unit"], hours["value"]) == ("My Hours This Week", "minutes", 120)
    # 5 October to 10 October: 240 minutes (the 60 of Sunday the 11th is outside the window).
    assert hours["comparison"] == {
        "basis": "previous_period",
        "previous": 240,
        "change": -120,
        "change_bp": -5_000,
    }
    assert hours["secondary"] == {"key": "target", "value": 2_250, "unit": "minutes"}
    assert hours["link"] == "/dashboard/my-timesheet"

    missing = cards["missing_timesheets"]
    assert (missing["value"], missing["unit"], missing["comparison"]) == (2, "count", None)
    assert missing["secondary"] == {"key": "people", "value": 2, "unit": "count"}

    assert cards["pending_approvals"]["value"] == 2  # Sam's timesheet and Olly's claim
    assert cards["pending_approvals"]["comparison"] is None

    wip = cards["unbilled_wip"]
    assert (wip["title"], wip["unit"], wip["value"]) == ("Unbilled WIP", "money", 22_300)
    # As at Sunday 11 October Gamma's 13 October time was not yet recorded.
    assert wip["comparison"] == {
        "basis": "period_start",
        "previous": 16_300,
        "change": 6_000,
        "change_bp": 3_681,
    }
    assert wip["secondary"] == {"key": "over_90_days", "value": 12_000, "unit": "money"}

    billed = cards["billed"]
    assert (billed["title"], billed["value"]) == ("Billed This Week", 0)
    assert billed["comparison"] == {
        "basis": "previous_period",
        "previous": 500,
        "change": -500,
        "change_bp": -10_000,
    }

    debtors = cards["overdue_debtors"]
    assert (debtors["title"], debtors["value"]) == ("Overdue Debtors", 3_080)
    # Invoice A fell due on the 16th: as at the 11th only C (1 760) and D (120) were overdue.
    assert debtors["comparison"] == {
        "basis": "period_start",
        "previous": 1_880,
        "change": 1_200,
        "change_bp": 6_383,
    }
    assert debtors["secondary"] == {"key": "total_outstanding", "value": 4_280, "unit": "money"}


async def test_cards_this_month(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = await _world(client, monkeypatch)
    data = await get_page(client, c.owner, c.org, "cards", period="this_month")
    assert (data["date_from"], data["date_to"]) == ("2026-10-01", "2026-10-17")
    assert (data["previous_from"], data["previous_to"]) == ("2026-09-01", "2026-09-17")
    cards = cards_by_key(data)

    hours = cards["my_hours"]
    assert (hours["title"], hours["value"]) == ("My Hours This Month", 420)
    assert hours["comparison"] == {
        "basis": "previous_period",
        "previous": 100,
        "change": 320,
        "change_bp": 32_000,
    }
    # The 22 working days of October at 7h30.
    assert hours["secondary"]["value"] == 22 * 450

    # Weeks of 5 and 28 September count: the owner (open) and Olly (none), and Olly (rejected).
    assert (
        cards["missing_timesheets"]["value"],
        cards["missing_timesheets"]["secondary"]["value"],
    ) == (3, 2)
    assert cards["pending_approvals"]["value"] == 2

    wip = cards["unbilled_wip"]
    assert wip["value"] == 22_300
    assert wip["comparison"] == {
        "basis": "period_start",
        "previous": 12_000,
        "change": 10_300,
        "change_bp": 8_583,
    }

    billed = cards["billed"]
    assert (billed["title"], billed["value"]) == ("Billed This Month", 1_500)
    assert billed["comparison"] == {
        "basis": "previous_period",
        "previous": 500,
        "change": 1_000,
        "change_bp": 20_000,
    }

    debtors = cards["overdue_debtors"]
    assert debtors["value"] == 3_080
    # As at 30 September: C 2 000 (before the credit note of 5 October) and D 120.
    assert debtors["comparison"] == {
        "basis": "period_start",
        "previous": 2_120,
        "change": 960,
        "change_bp": 4_528,
    }


async def test_cards_year_to_date(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = await _world(client, monkeypatch)
    data = await get_page(client, c.owner, c.org, "cards", period="ytd")
    assert (data["date_from"], data["date_to"]) == ("2026-01-01", "2026-10-17")
    assert (data["previous_from"], data["previous_to"]) == ("2025-01-01", "2025-10-17")
    cards = cards_by_key(data)

    hours = cards["my_hours"]
    assert (hours["title"], hours["value"]) == ("My Hours Year to Date", 520)
    assert hours["comparison"] == {
        "basis": "previous_period",
        "previous": 0,
        "change": 520,
        "change_bp": None,
    }
    # Up to today, from the day the owner started (14 September).
    assert hours["secondary"]["value"] == _working_days(START, TODAY) * 450

    # Olly: 21 and 28 September and 5 October; the owner: 5 October.
    assert (
        cards["missing_timesheets"]["value"],
        cards["missing_timesheets"]["secondary"]["value"],
    ) == (4, 2)
    wip = cards["unbilled_wip"]
    assert wip["comparison"] == {
        "basis": "period_start",
        "previous": 0,
        "change": 22_300,
        "change_bp": None,
    }
    billed = cards["billed"]
    assert (billed["title"], billed["value"]) == ("Billed Year to Date", 4_100)
    assert billed["comparison"]["previous"] == 0
    assert cards["overdue_debtors"]["comparison"]["previous"] == 0


async def test_the_default_period_is_this_week(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    data = await get_page(client, c.owner, c.org, "cards")
    assert data["period"] == "this_week"
    assert cards_by_key(data)["my_hours"]["title"] == "My Hours This Week"
    bad = await get_page(client, c.owner, c.org, "cards", status=422, period="decade")
    assert bad["code"] == "VALIDATION_ERROR"


async def test_the_week_follows_the_practice_week_start(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    await set_org(c.org, week_start=7)  # weeks run Sunday to Saturday
    data = await get_page(client, c.owner, c.org, "cards", period="this_week")
    assert (data["date_from"], data["date_to"]) == ("2026-10-11", "2026-10-17")
    assert (data["previous_from"], data["previous_to"]) == ("2026-10-04", "2026-10-10")
    # Sunday 11th (60) to the 13th: 60 + 90 + 30; the target is the 5 working days of the week.
    hours = cards_by_key(data)["my_hours"]
    assert (hours["value"], hours["comparison"]["previous"]) == (180, 240)
    assert hours["secondary"]["value"] == 5 * 450


async def test_today_is_the_practice_date(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    await set_org(c.org, timezone="America/New_York")  # still Friday evening there
    data = await get_page(client, c.owner, c.org, "cards", period="this_month")
    assert (data["as_at"], data["date_to"]) == ("2026-10-16", "2026-10-16")
    assert (data["previous_from"], data["previous_to"]) == ("2026-09-01", "2026-09-16")


async def test_visibility_by_role(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = await _world(client, monkeypatch)
    expected = {
        "staff": {"my_hours"},
        "manager": set(ORDER),
        "partner": set(ORDER),
        # Read-only: no time of their own, nothing to approve.
        "viewer": {"missing_timesheets", "unbilled_wip", "billed", "overdue_debtors"},
        "billing": {"missing_timesheets", "unbilled_wip", "billed", "overdue_debtors"},
    }
    for role, visible in expected.items():
        person = await add_member(c.org["id"], role)
        data = await get_page(client, person, c.org, "cards")
        cards = cards_by_key(data)
        assert [card["key"] for card in data["cards"]] == ORDER, role
        assert {key for key, card in cards.items() if card["visible"]} == visible, role
        for key, card in cards.items():
            if not card["visible"]:
                assert (card["value"], card["comparison"], card["secondary"]) == (None, None, None)
                assert card["link"].startswith("/dashboard/"), (role, key)


async def test_one_permission_unlocks_its_cards(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    expected = {
        "WIP only": ({"wip.read"}, {"unbilled_wip"}),
        "Invoices only": ({"invoice.read"}, {"billed", "overdue_debtors"}),
        "Payments only": ({"payment.read"}, {"overdue_debtors"}),
        "Financial": ({"report.financial"}, {"unbilled_wip", "billed", "overdue_debtors"}),
        "Approver": ({"timesheet.approve"}, {"missing_timesheets", "pending_approvals"}),
        "Expense approver": ({"expense.approve"}, {"pending_approvals"}),
        "Reads time": ({"time.read_all"}, {"missing_timesheets"}),
        "Logs time": ({"time.log"}, {"my_hours"}),
        "Nothing": ({"organization.read"}, set()),
    }
    for name, (codes, visible) in expected.items():
        await custom_role(c.org, name, codes)
        person = await add_member(c.org["id"], name)
        data = await get_page(client, person, c.org, "cards")
        shown = {card["key"] for card in data["cards"] if card["visible"]}
        assert shown == visible, name


async def test_an_approver_sees_the_people_they_manage(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    await custom_role(c.org, "Lead", {"timesheet.approve", "expense.approve", "time.log"})
    lead = await add_member(c.org["id"], "Lead", full_name="Lee Lead")
    await set_member(c.org, lead, start_date=START)
    await set_member(c.org, c.sam, manager_user_id=lead.id)
    cards = cards_by_key(await get_page(client, lead, c.org, "cards"))
    # Sam's submitted timesheet (Olly's claim is not Lee's to decide on); the team is Sam
    # (submitted) and Lee, whose own timesheet of 5 October is missing.
    assert cards["pending_approvals"]["value"] == 1
    assert cards["missing_timesheets"]["value"] == 1
    assert cards["missing_timesheets"]["secondary"]["value"] == 1


async def test_own_submissions_are_not_waiting_for_oneself(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    # Mia holds timesheet.approve but not organization.update: her own sheet is not hers to decide.
    await make_timesheet(c.org, c.mia, date(2026, 9, 28), "submitted")
    assert (
        cards_by_key(await get_page(client, c.mia, c.org, "cards"))["pending_approvals"]["value"]
        == 2
    )
    # The owner may approve their own (organization.update), so it counts for them.
    await make_timesheet(c.org, c.owner, date(2026, 9, 28), "submitted")
    assert (
        cards_by_key(await get_page(client, c.owner, c.org, "cards"))["pending_approvals"]["value"]
        == 4
    )


async def test_an_empty_practice(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    pin_clock(monkeypatch)
    owner, org = await practice(client)
    data = await get_page(client, owner, org, "cards", period="this_month")
    cards = cards_by_key(data)
    assert all(card["visible"] for card in cards.values())
    for key in ("my_hours", "pending_approvals", "unbilled_wip", "billed", "overdue_debtors"):
        assert cards[key]["value"] == 0, key
    for key in ("my_hours", "unbilled_wip", "billed", "overdue_debtors"):
        comparison = cards[key]["comparison"]
        assert (comparison["previous"], comparison["change"], comparison["change_bp"]) == (
            0,
            0,
            None,
        )
    # The owner has no start date, so they are expected every week: nothing submitted for
    # the weeks of 28 September and 5 October.
    assert cards["missing_timesheets"]["value"] == 2


async def test_tenant_isolation(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    c, _ = await _world(client, monkeypatch)
    other, other_org = await practice(client, "Other & Co")
    refused = await get_page(client, other, c.org, "cards", status=404)
    assert refused["code"] == "RESOURCE_NOT_FOUND"
    cards = cards_by_key(await get_page(client, other, other_org, "cards", period="ytd"))
    for key in ("my_hours", "pending_approvals", "unbilled_wip", "billed", "overdue_debtors"):
        assert cards[key]["value"] == 0, key
    outsider = await get_page(client, c.owner, other_org, "cards", status=404)
    assert outsider["code"] == "RESOURCE_NOT_FOUND"


async def test_the_cards_use_a_fixed_number_of_queries(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _ = await _world(client, monkeypatch)
    statements: list[str] = []

    def count(conn, cursor, statement, *args) -> None:
        statements.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        await get_page(client, c.owner, c.org, "cards", period="ytd")
        first = len(statements)
        beta = await make_client(c.org, "More Ltd")
        for day in range(1, 8):
            await make_time_entry(
                c.org,
                c.sam,
                client=beta,
                entry_date=date(2026, 9, day),
                timesheet_status="approved",
            )
        statements.clear()
        await get_page(client, c.owner, c.org, "cards", period="ytd")
        assert len(statements) == first
        assert first <= 30
    finally:
        event.remove(engine, "before_cursor_execute", count)
