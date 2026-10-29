"""Period locks by week, month, quarter or accounting year: the practice's lock length and
accounting year start, the periods of a year, and that a new lock is exactly one such period."""

import uuid
from datetime import date, timedelta
from itertools import pairwise

import pytest
from httpx import AsyncClient, Response
from sqlalchemy.exc import IntegrityError

from app.db.session import get_sessionmaker
from app.services import period_locks
from app.utils.lock_periods import (
    accounting_year_of,
    period_bounds,
    period_label,
    year_bounds,
    year_label,
    year_periods,
)
from tests.clients_helpers import sql
from tests.domain_factories import practice, staff
from tests.factories import audit_entries, error
from tests.period_lock_helpers import (
    events,
    lock_period,
    locked_periods,
    org_url,
    set_granularity,
    set_lock,
    today,
    unlock_period,
)

DAY = timedelta(days=1)


async def calendar(client: AsyncClient, actor, org: dict, **params) -> dict:
    response = await client.get(
        org_url(org, "/period-locks/calendar"), headers=actor.headers, params=params
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def put_settings(client: AsyncClient, actor, org: dict, **body) -> Response:
    return await client.put(
        org_url(org, "/period-locks/settings"), headers=actor.headers, json=body
    )


def refused(response: Response, code: str) -> None:
    assert response.status_code == 422, response.text
    assert error(response)["code"] == code, error(response)


# --- The periods of an accounting year ---------------------------------------------------------


def test_a_calendar_year_has_52_or_53_monday_weeks_that_begin_in_it():
    for year, count in ((2023, 52), (2024, 53), (2026, 52)):
        weeks = year_periods("week", year)
        assert len(weeks) == count, year
        assert all(start.weekday() == 0 and end - start == 6 * DAY for _, start, end in weeks)
        assert all(start.year == year for _, start, _ in weeks)
        assert [b - a for (_, a, _), (_, b, _) in pairwise(weeks)] == [7 * DAY] * (count - 1)
    # The first Monday of 2023 is the 2nd; the last week of 2024 runs into 2025.
    assert period_bounds("week", 2023, 1) == (date(2023, 1, 2), date(2023, 1, 8))
    assert period_bounds("week", 2024, 53) == (date(2024, 12, 30), date(2025, 1, 5))
    with pytest.raises(ValueError, match="52 weeks"):
        period_bounds("week", 2023, 53)


def test_weeks_follow_the_practices_first_day_of_the_week():
    # Sunday-start weeks (7) of the 6 April 2026 year.
    first = period_bounds("week", 2026, 1, start_month=4, start_day=6, week_start=7)
    assert first == (date(2026, 4, 12), date(2026, 4, 18))


def test_a_year_that_starts_on_6_april_has_months_and_quarters_from_that_day():
    kwargs = {"start_month": 4, "start_day": 6}
    assert year_bounds(2026, 4, 6) == (date(2026, 4, 6), date(2027, 4, 5))
    assert period_bounds("year", 2026, None, **kwargs) == (date(2026, 4, 6), date(2027, 4, 5))
    assert period_bounds("quarter", 2026, 1, **kwargs) == (date(2026, 4, 6), date(2026, 7, 5))
    assert period_bounds("quarter", 2026, 4, **kwargs) == (date(2027, 1, 6), date(2027, 4, 5))
    assert period_bounds("month", 2026, 1, **kwargs) == (date(2026, 4, 6), date(2026, 5, 5))
    assert period_bounds("month", 2026, 12, **kwargs) == (date(2027, 3, 6), date(2027, 4, 5))
    # The periods tile the year: each starts the day after the one before ends.
    for kind in ("month", "quarter", "week"):
        rows = year_periods(kind, 2026, **kwargs)
        for (_, _, end), (_, start, _) in pairwise(rows):
            assert start == end + DAY or kind == "week"
    months = year_periods("month", 2026, **kwargs)
    assert months[0][1] == date(2026, 4, 6) and months[-1][2] == date(2027, 4, 5)
    assert accounting_year_of(date(2026, 4, 5), 4, 6) == 2025
    assert accounting_year_of(date(2026, 4, 6), 4, 6) == 2026
    assert year_label(2026, 4, 6) == "2026/27" and year_label(2026) == "2026"


@pytest.mark.parametrize(
    ("kind", "year", "index"),
    [
        ("week", 2023, 0),
        ("week", 2023, 54),
        ("month", 2023, 13),
        ("quarter", 2023, 5),
        ("day", 2023, 1),
    ],
)
def test_an_index_outside_the_year_is_refused(kind, year, index):
    with pytest.raises(ValueError):
        period_bounds(kind, year, index)


def test_labels():
    assert period_label("week", date(2026, 4, 6), date(2026, 4, 12)) == "Week of 06/04/2026"
    assert period_label("month", date(2026, 3, 1), date(2026, 3, 31)) == "March 2026"
    assert period_label("quarter", date(2026, 1, 1), date(2026, 3, 31)) == "Q1 2026"
    assert period_label("quarter", date(2026, 4, 6), date(2026, 7, 5)) == "06/04/2026 to 05/07/2026"
    assert period_label("year", date(2026, 4, 6), date(2027, 4, 5)) == "2026/27"
    assert period_label("year", date(2026, 1, 1), date(2026, 12, 31)) == "2026"


# --- The setting -------------------------------------------------------------------------------


async def test_the_default_is_weekly_in_a_calendar_year_and_unconfirmed(client):
    owner, org = await practice(client)
    state = (await client.get(org_url(org, "/period-locks"), headers=owner.headers)).json()["data"]
    assert state["settings"] == {
        "granularity": "weekly",
        "financial_year_start_month": 1,
        "financial_year_start_day": 1,
        "configured": False,
    }


async def test_choosing_how_to_lock_is_saved_confirmed_and_audited(client):
    owner, org = await practice(client)
    response = await put_settings(
        client,
        owner,
        org,
        granularity="quarterly",
        financial_year_start_month=4,
        financial_year_start_day=6,
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["settings"] == {
        "granularity": "quarterly",
        "financial_year_start_month": 4,
        "financial_year_start_day": 6,
        "configured": True,
    }
    [entry] = await audit_entries(org["id"], "period_lock.settings_changed")
    assert entry["metadata"]["changes"] == {
        "period_lock_granularity": ["weekly", "quarterly"],
        "period_lock_configured": [False, True],
        "financial_year_start_month": [1, 4],
        "financial_year_start_day": [1, 6],
    }
    # Left out, the accounting year start stays; saying the same again changes nothing.
    again = await put_settings(client, owner, org, granularity="monthly")
    assert again.json()["data"]["settings"]["financial_year_start_month"] == 4
    assert (await put_settings(client, owner, org, granularity="monthly")).status_code == 200
    assert len(await audit_entries(org["id"], "period_lock.settings_changed")) == 2


async def test_weekly_can_be_confirmed_as_it_is(client):
    owner, org = await practice(client)
    response = await put_settings(client, owner, org, granularity="weekly")
    assert response.json()["data"]["settings"]["configured"] is True


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"granularity": "daily"},
        {"granularity": ""},
        {"granularity": "weekly", "financial_year_start_month": 13},
        {"granularity": "weekly", "financial_year_start_month": 0},
        {"granularity": "weekly", "financial_year_start_day": 29},
        {"granularity": "weekly", "financial_year_start_day": 0},
        {"granularity": "weekly", "financial_year_start_month": "April"},
    ],
)
async def test_settings_body_validation(client, body):
    owner, org = await practice(client)
    response = await client.put(
        org_url(org, "/period-locks/settings"), headers=owner.headers, json=body
    )
    assert response.status_code == 422, response.text
    assert await audit_entries(org["id"], "period_lock.settings_changed") == []


@pytest.mark.parametrize(
    ("role", "allowed"),
    [("owner", True), ("admin", True), ("partner", True), ("manager", False), ("viewer", False)],
)
async def test_changing_the_setting_needs_period_lock_manage(client, role, allowed):
    owner, org = await practice(client)
    actor = owner if role == "owner" else await staff(org, role)
    response = await put_settings(client, actor, org, granularity="monthly")
    assert response.status_code == (200 if allowed else 403), response.text


# --- Locking one period of the chosen length ---------------------------------------------------


@pytest.mark.parametrize("granularity", ["weekly", "monthly", "quarterly", "annually"])
async def test_only_the_chosen_length_can_be_locked(client, granularity):
    owner, org = await practice(client)
    await put_settings(client, owner, org, granularity=granularity)
    kinds = {"weekly": "week", "monthly": "month", "quarterly": "quarter", "annually": "year"}
    year = 2023
    for kind in kinds.values():
        response = await lock_period(client, owner, org, kind, year, 1, align=False)
        if kind == kinds[granularity]:
            assert response.status_code == 200, response.text
            [stored] = await locked_periods(org)
            assert stored["period_type"] == kind
        else:
            refused(response, "PERIOD_LOCK_WRONG_LENGTH")
            assert "Settings > Period Lock" in error(response)["message"]
    assert len(await locked_periods(org)) == 1 and len(await events(org)) == 1


async def test_a_new_practice_locks_weeks_not_months(client):
    owner, org = await practice(client)
    refused(
        await lock_period(client, owner, org, "month", 2023, 1, align=False),
        "PERIOD_LOCK_WRONG_LENGTH",
    )
    response = await lock_period(client, owner, org, "week", 2023, 1, align=False, reason="Week")
    assert response.status_code == 200, response.text
    [locked] = response.json()["data"]["periods"]
    assert locked["period_type"] == "week"
    assert (locked["start_date"], locked["end_date"]) == ("2023-01-02", "2023-01-08")
    assert locked["label"] == "Week of 02/01/2023"
    [entry] = await audit_entries(org["id"], "period_lock.locked")
    assert entry["metadata"]["period_type"] == "week"


async def test_a_week_is_numbered_from_the_start_of_the_accounting_year(client):
    owner, org = await practice(client)
    await put_settings(
        client,
        owner,
        org,
        granularity="weekly",
        financial_year_start_month=4,
        financial_year_start_day=6,
    )
    # 6 April 2023 is a Thursday: week 1 is Monday 10 April to Sunday 16 April.
    response = await lock_period(client, owner, org, "week", 2023, 1, align=False)
    assert response.status_code == 200, response.text
    assert [(p["start_date"], p["end_date"]) for p in response.json()["data"]["periods"]] == [
        ("2023-04-10", "2023-04-16")
    ]


async def test_a_week_the_year_does_not_have_is_refused(client):
    owner, org = await practice(client)
    refused(
        await lock_period(client, owner, org, "week", 2023, 53, align=False),
        "PERIOD_LOCK_INVALID_PERIOD",
    )
    assert (await lock_period(client, owner, org, "week", 2024, 53, align=False)).status_code == 200


async def test_a_quarter_is_counted_from_the_accounting_year_start(client):
    owner, org = await practice(client)
    await put_settings(
        client,
        owner,
        org,
        granularity="quarterly",
        financial_year_start_month=4,
        financial_year_start_day=6,
    )
    response = await lock_period(client, owner, org, "quarter", 2023, 1, align=False)
    assert response.status_code == 200, response.text
    [locked] = response.json()["data"]["periods"]
    assert (locked["start_date"], locked["end_date"]) == ("2023-04-06", "2023-07-05")
    assert locked["label"] == "06/04/2023 to 05/07/2023"


async def test_the_current_week_cannot_be_locked_yet(client):
    owner, org = await practice(client)
    t = today(org)
    this_year = accounting_year_of(t)
    weeks = year_periods("week", this_year, week_start=org["week_start"])
    current = next(i for i, start, end in weeks if start <= t <= end)
    refused(
        await lock_period(client, owner, org, "week", this_year, current, align=False),
        "PERIOD_LOCK_IN_FUTURE",
    )
    if current > 1:
        assert (
            await lock_period(client, owner, org, "week", this_year, current - 1, align=False)
        ).status_code == 200


async def test_a_lock_made_before_the_length_changed_stays_in_force_and_can_be_unlocked(client):
    owner, org = await practice(client)
    assert (await lock_period(client, owner, org, "year", 2022)).status_code == 200
    await put_settings(client, owner, org, granularity="weekly")
    org_id = uuid.UUID(org["id"])
    async with get_sessionmaker()() as session:
        with pytest.raises(period_locks.PeriodLockedError):
            await period_locks.assert_open(session, org_id, date(2022, 6, 1), "time")
    # A week inside the locked year overlaps it.
    refused(
        await lock_period(client, owner, org, "week", 2022, 10, align=False), "PERIOD_LOCK_OVERLAPS"
    )
    state = (await client.get(org_url(org, "/period-locks"), headers=owner.headers)).json()["data"]
    [year_lock] = state["periods"]
    assert year_lock["period_type"] == "year"
    response = await unlock_period(client, owner, org, year_lock["id"], reason="Re-open")
    assert response.status_code == 200, response.text


async def test_a_legacy_lock_still_covers_what_it_did(client):
    _, org = await practice(client)
    await set_lock(org, date(2023, 8, 31))
    async with get_sessionmaker()() as session:
        with pytest.raises(period_locks.PeriodLockedError):
            await period_locks.assert_open(session, uuid.UUID(org["id"]), date(2023, 8, 30), "wip")
        await period_locks.assert_open(session, uuid.UUID(org["id"]), date(2023, 9, 1), "wip")


# --- The calendar ------------------------------------------------------------------------------


async def test_the_default_calendar_is_the_52_weeks_of_this_year_with_lock_state(client):
    owner, org = await practice(client)
    t = today(org)
    data = await calendar(client, owner, org)
    assert data["year"] == accounting_year_of(t) == t.year
    assert data["label"] == str(t.year)
    assert (data["start_date"], data["end_date"]) == (f"{t.year}-01-01", f"{t.year}-12-31")
    rows = data["periods"]
    assert len(rows) in (52, 53)
    assert [r["period"] for r in rows] == list(range(1, len(rows) + 1))
    assert {r["period_type"] for r in rows} == {"week"} and {r["year"] for r in rows} == {t.year}
    for row in rows:
        ended = date.fromisoformat(row["end_date"]) < t + DAY
        assert row["status"] == ("open" if ended else "future"), row
        assert row["lock_id"] is None and row["blocked_by"] is None


async def test_locking_a_row_of_the_calendar_marks_it_and_unlocking_it_reopens_it(client):
    owner, org = await practice(client)
    rows = (await calendar(client, owner, org, year=2023))["periods"]
    assert len(rows) == 52
    week = rows[9]
    assert week["status"] == "open" and week["label"].startswith("Week of ")
    response = await lock_period(
        client, owner, org, week["period_type"], week["year"], week["period"], align=False
    )
    assert response.status_code == 200, response.text
    data = await calendar(client, owner, org, year=2023)
    locked = data["periods"][9]
    assert locked["status"] == "locked" and locked["locked_by_name"] and locked["locked_at"]
    assert [r["status"] for r in data["periods"]].count("locked") == 1
    unlocked = await unlock_period(client, owner, org, locked["lock_id"], reason="Fix")
    assert unlocked.status_code == 200, unlocked.text
    assert (await calendar(client, owner, org, year=2023))["periods"][9]["status"] == "open"


async def test_a_row_that_another_lock_overlaps_is_blocked_and_names_it(client):
    owner, org = await practice(client)
    assert (await lock_period(client, owner, org, "quarter", 2023, 1)).status_code == 200
    await put_settings(client, owner, org, granularity="monthly")
    rows = (await calendar(client, owner, org, year=2023))["periods"]
    assert len(rows) == 12
    assert [r["status"] for r in rows[:4]] == ["blocked", "blocked", "blocked", "open"]
    assert rows[0]["blocked_by"] == "Q1 2023" and rows[0]["lock_id"] is None


async def test_the_calendar_follows_the_accounting_year(client):
    owner, org = await practice(client)
    await put_settings(
        client,
        owner,
        org,
        granularity="annually",
        financial_year_start_month=4,
        financial_year_start_day=6,
    )
    data = await calendar(client, owner, org, year=2024)
    assert data["label"] == "2024/25"
    assert (data["start_date"], data["end_date"]) == ("2024-04-06", "2025-04-05")
    [row] = data["periods"]
    assert (row["start_date"], row["end_date"], row["label"]) == (
        "2024-04-06",
        "2025-04-05",
        "2024/25",
    )
    assert (await lock_period(client, owner, org, "year", 2024, align=False)).status_code == 200
    assert (await calendar(client, owner, org, year=2024))["periods"][0]["status"] == "locked"


async def test_the_years_are_listed_newest_first_with_how_many_periods_are_locked(client):
    owner, org = await practice(client)
    t = today(org)
    for week in (1, 2, 3):
        assert (await lock_period(client, owner, org, "week", t.year - 1, week)).status_code == 200
    data = await calendar(client, owner, org)
    years = data["years"]
    assert [y["year"] for y in years] == list(range(t.year, t.year - 6, -1))
    by_year = {y["year"]: y for y in years}
    assert by_year[t.year - 1]["locked"] == 3 and by_year[t.year]["locked"] == 0
    assert by_year[t.year - 1]["total"] in (52, 53)
    assert by_year[t.year]["label"] == str(t.year)


async def test_years_reach_back_to_the_oldest_lock_but_not_to_a_legacy_one(client):
    owner, org = await practice(client)
    await set_lock(org, date(2019, 3, 31))
    t = today(org)
    assert (await calendar(client, owner, org))["years"][-1]["year"] == t.year - 5
    await set_lock(org, None)
    await put_settings(client, owner, org, granularity="annually")
    assert (
        await lock_period(client, owner, org, "year", t.year - 9, align=False)
    ).status_code == 200
    assert (await calendar(client, owner, org))["years"][-1]["year"] == t.year - 9


@pytest.mark.parametrize("year", [1999, 2101, "x"])
async def test_the_calendar_year_is_validated(client, year):
    owner, org = await practice(client)
    response = await client.get(
        org_url(org, "/period-locks/calendar"), headers=owner.headers, params={"year": year}
    )
    assert response.status_code == 422


async def test_the_calendar_needs_period_lock_read(client):
    _, org = await practice(client)
    for role, expected in (("manager", 200), ("staff", 403)):
        actor = await staff(org, role)
        response = await client.get(org_url(org, "/period-locks/calendar"), headers=actor.headers)
        assert response.status_code == expected, role


# --- Existing practices ------------------------------------------------------------------------


async def test_set_granularity_helper_writes_the_columns(client):
    _, org = await practice(client)
    await set_granularity(org, "quarterly", (4, 6))
    [row] = await sql(
        "SELECT period_lock_granularity AS g, financial_year_start_month AS m, "
        "financial_year_start_day AS d, period_lock_configured AS c "
        "FROM organizations WHERE id = :o",
        o=org["id"],
    )
    assert row == {"g": "quarterly", "m": 4, "d": 6, "c": True}


@pytest.mark.parametrize(
    "values",
    [
        "period_lock_granularity = 'daily'",
        "financial_year_start_month = 13",
        "financial_year_start_day = 29",
        "financial_year_start_day = 0",
    ],
)
async def test_the_database_refuses_a_bad_setting(client, values):
    _, org = await practice(client)
    with pytest.raises(IntegrityError):
        await sql(f"UPDATE organizations SET {values} WHERE id = :o", o=org["id"])  # noqa: S608
