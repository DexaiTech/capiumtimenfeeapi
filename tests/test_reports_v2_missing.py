"""The missing-timesheets report: one row per person and week, the same people as the screen
(``GET /timesheets/missing``), the week range, paging and access."""

from datetime import date
from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from tests.domain_factories import practice, staff
from tests.factories import add_member, error
from tests.report_helpers import call, get_data
from tests.reports_helpers import custom_role
from tests.time_gap_helpers import day, missing, pin_today, run_reminders, set_member, sheet_action
from tests.time_helpers import log_time, set_manager

# A Wednesday: the week of Monday 7 September is the last completed week.
OVER = date(2026, 9, 16)
NAME = "missing-timesheets"
FIELDS = ("name", "week_start", "status", "logged_minutes", "expected_minutes", "shortfall_minutes")


async def office(client: AsyncClient, monkeypatch) -> SimpleNamespace:
    """The owner is not asked for a timesheet. Ann has nothing, Bob logged an hour and did not
    submit, Cat submitted, Dan was returned, Eve was approved and Jan started on the Wednesday."""
    pin_today(monkeypatch, OVER)
    owner, org = await practice(client)
    await set_member(org, owner, weekly_capacity_minutes=0)
    ann = await staff(org, full_name="Ann Adams")
    bob = await staff(org, full_name="Bob Brown")
    cat = await staff(org, full_name="Cat Clark")
    dan = await staff(org, full_name="Dan Dale")
    eve = await staff(org, full_name="Eve East")
    jan = await staff(org, full_name="Jan Jones")
    await set_member(org, jan, start_date=date(2026, 9, 9))
    await log_time(client, bob, org, minutes=60)
    for person in (cat, dan, eve):
        entry = await log_time(client, person, org, minutes=60)
        await sheet_action(client, person, org, entry["timesheet_id"], "submit")
        if person is dan:
            await sheet_action(
                client,
                owner,
                org,
                entry["timesheet_id"],
                "reject",
                json={"decision_note": "Detail"},
            )
        if person is eve:
            await sheet_action(client, owner, org, entry["timesheet_id"], "approve")
    return SimpleNamespace(
        owner=owner, org=org, ann=ann, bob=bob, cat=cat, dan=dan, eve=eve, jan=jan
    )


def shape(rows: list[dict]) -> list[tuple]:
    return [tuple(row[field] for field in FIELDS) for row in rows]


async def test_the_last_completed_week_by_default(client: AsyncClient, monkeypatch) -> None:
    o = await office(client, monkeypatch)
    data = await get_data(client, o.owner, o.org, NAME)
    assert shape(data["rows"]) == [
        ("Ann Adams", day(0), "none", 0, 2250, 2250),
        ("Bob Brown", day(0), "open", 60, 2250, 2190),
        ("Dan Dale", day(0), "rejected", 60, 2250, 2190),
        ("Jan Jones", day(0), "none", 0, 1350, 1350),  # started on the Wednesday
    ]
    ann, bob, _, jan = data["rows"]
    assert ann["user_id"] == str(o.ann.id)
    assert ann["timesheet_id"] is None and bob["timesheet_id"]
    assert ann["missing_days"] == [day(i) for i in range(5)]
    assert (ann["missing_day_count"], bob["missing_day_count"], jan["missing_day_count"]) == (
        5,
        4,
        3,
    )
    assert bob["missing_days"] == [day(i) for i in range(1, 5)]
    assert ann["has_login"] is True and ann["last_reminded_at"] is None
    assert data["totals"] == {
        "timesheets": 4,
        "people": 4,
        "weeks": 1,
        "none_count": 2,
        "open_count": 1,
        "rejected_count": 1,
        "logged_minutes": 120,
        "expected_minutes": 8_100,
        "shortfall_minutes": 7_980,
        "missing_day_count": 16,
    }
    assert data["filters"]["week_from"] == data["filters"]["week_to"] == day(0)
    assert (data["filters"]["date_from"], data["filters"]["date_to"]) == (day(0), day(6))
    assert data["page"]["total"] == 4


async def test_the_rows_are_the_screens_rows(client: AsyncClient, monkeypatch) -> None:
    """Report figures equal the Missing Timesheets screen's for the same week (2.17)."""
    o = await office(client, monkeypatch)
    for week in (day(0), day(7)):
        screen = await missing(client, o.owner, o.org, week_start=week)
        data = await get_data(client, o.owner, o.org, NAME, week_from=week, week_to=week)
        assert data["rows"] == [
            {
                **row,
                "shortfall_minutes": max(row["expected_minutes"] - row["logged_minutes"], 0),
                "missing_day_count": len(row["missing_days"]),
            }
            for row in screen
        ], week


async def test_several_weeks_newest_first(client: AsyncClient, monkeypatch) -> None:
    o = await office(client, monkeypatch)
    data = await get_data(client, o.owner, o.org, NAME, week_from=day(-7), week_to=day(3))
    weeks = [(r["week_start"], r["name"]) for r in data["rows"]]
    # Sorted by week (newest first), then by name; the week before had nobody submit.
    assert weeks == [
        *[(day(0), n) for n in ("Ann Adams", "Bob Brown", "Dan Dale", "Jan Jones")],
        *[(day(-7), n) for n in ("Ann Adams", "Bob Brown", "Cat Clark", "Dan Dale", "Eve East")],
    ]
    totals = data["totals"]
    assert (totals["timesheets"], totals["people"], totals["weeks"]) == (9, 6, 2)
    assert (totals["none_count"], totals["open_count"], totals["rejected_count"]) == (7, 1, 1)
    assert totals["expected_minutes"] == 8_100 + 5 * 2250
    assert totals["missing_day_count"] == 16 + 5 * 5
    assert (data["filters"]["date_from"], data["filters"]["date_to"]) == (day(-7), day(6))
    # Each week keeps the screen's rows.
    for week in (day(-7), day(0)):
        listed = [r["user_id"] for r in data["rows"] if r["week_start"] == week]
        assert listed == [
            r["user_id"] for r in await missing(client, o.owner, o.org, week_start=week)
        ]


async def test_week_defaults(client: AsyncClient, monkeypatch) -> None:
    o = await office(client, monkeypatch)
    # Only week_from: up to the last completed week, or just that week when it is later.
    from_current = await get_data(client, o.owner, o.org, NAME, week_from=day(7))
    assert from_current["totals"]["weeks"] == 1
    assert {r["week_start"] for r in from_current["rows"]} == {day(7)}
    assert len(from_current["rows"]) == 6  # everyone employed that week, nothing submitted yet
    assert from_current["rows"][0]["missing_days"] == [day(7), day(8), day(9)]  # up to today
    earlier = await get_data(client, o.owner, o.org, NAME, week_from=day(-14))
    assert earlier["filters"]["week_from"] == day(-14) and earlier["filters"]["week_to"] == day(0)
    assert earlier["totals"]["weeks"] == 3
    # Only week_to: that single week; any day names the week.
    only_to = await get_data(client, o.owner, o.org, NAME, week_to=day(3))
    assert only_to["filters"]["week_from"] == only_to["filters"]["week_to"] == day(0)


async def test_status_filter(client: AsyncClient, monkeypatch) -> None:
    o = await office(client, monkeypatch)
    rejected = await get_data(client, o.owner, o.org, NAME, status=["rejected"])
    assert [r["name"] for r in rejected["rows"]] == ["Dan Dale"]
    assert rejected["totals"]["rejected_count"] == 1 and rejected["totals"]["none_count"] == 0
    both = await get_data(client, o.owner, o.org, NAME, status=["open", "rejected"])
    assert [r["name"] for r in both["rows"]] == ["Bob Brown", "Dan Dale"]
    assert both["filters"]["status"] == ["open", "rejected"]


async def test_person_and_manager_filters(client: AsyncClient, monkeypatch) -> None:
    o = await office(client, monkeypatch)
    lead = await staff(o.org, full_name="Lena Lead")
    await set_member(o.org, lead, weekly_capacity_minutes=0)
    await set_manager(o.org, o.ann, lead)
    await set_manager(o.org, o.bob, lead)
    by_person = await get_data(client, o.owner, o.org, NAME, user_id=o.ann.id)
    assert [r["name"] for r in by_person["rows"]] == ["Ann Adams"]
    by_manager = await get_data(client, o.owner, o.org, NAME, manager_user_id=lead.id)
    assert [r["name"] for r in by_manager["rows"]] == ["Ann Adams", "Bob Brown"]
    assert by_manager["filters"]["manager_user_id"] == str(lead.id)


async def test_paging(client: AsyncClient, monkeypatch) -> None:
    o = await office(client, monkeypatch)
    everything = await get_data(client, o.owner, o.org, NAME, week_from=day(-7), week_to=day(0))
    first = await get_data(client, o.owner, o.org, NAME, week_from=day(-7), week_to=day(0), limit=4)
    second = await get_data(
        client, o.owner, o.org, NAME, week_from=day(-7), week_to=day(0), limit=4, offset=4
    )
    last = await get_data(
        client, o.owner, o.org, NAME, week_from=day(-7), week_to=day(0), limit=4, offset=8
    )
    assert first["page"] == {
        "total": 9,
        "limit": 4,
        "offset": 0,
        "has_more": True,
        "next_offset": 4,
    }
    assert second["page"]["next_offset"] == 8 and second["page"]["has_more"] is True
    assert last["page"] == {
        "total": 9,
        "limit": 4,
        "offset": 8,
        "has_more": False,
        "next_offset": None,
    }
    assert first["rows"] + second["rows"] + last["rows"] == everything["rows"]
    assert len(last["rows"]) == 1
    # The totals always cover the whole report.
    assert first["totals"] == second["totals"] == everything["totals"]


async def test_reminders_show_when_sent(client: AsyncClient, monkeypatch) -> None:
    o = await office(client, monkeypatch)
    sent = await run_reminders(client, o.owner, o.org)
    assert sent["reminded"] == 4
    data = await get_data(client, o.owner, o.org, NAME)
    assert all(row["last_reminded_at"] for row in data["rows"])


async def test_access_follows_the_screen(client: AsyncClient, monkeypatch) -> None:
    o = await office(client, monkeypatch)
    viewer = await staff(o.org, "viewer")  # report.read and time.read_all
    await set_member(o.org, viewer, weekly_capacity_minutes=0)
    assert len((await get_data(client, viewer, o.org, NAME))["rows"]) == 4
    # Without time.read_all: the people the caller manages, and nobody else.
    lead = await staff(o.org, full_name="Lena Lead")
    await set_member(o.org, lead, weekly_capacity_minutes=0)
    await set_manager(o.org, o.ann, lead)
    await set_manager(o.org, o.bob, lead)
    assert [r["name"] for r in (await get_data(client, lead, o.org, NAME))["rows"]] == [
        "Ann Adams",
        "Bob Brown",
    ]
    assert (await call(client, lead, o.org, NAME, user_id=o.ann.id)).status_code == 200
    refused = await call(client, lead, o.org, NAME, user_id=o.dan.id)
    assert refused.status_code == 403 and error(refused)["code"] == "PERMISSION_DENIED"
    assert (await call(client, lead, o.org, NAME, manager_user_id=o.owner.id)).status_code == 403
    stranger = await staff(o.org, full_name="Sam Stranger")
    await set_member(o.org, stranger, weekly_capacity_minutes=0)
    assert (await get_data(client, stranger, o.org, NAME))["rows"] == []  # manages nobody
    # One's own status is one's own to see.
    await set_member(o.org, stranger, weekly_capacity_minutes=2250)
    own = await get_data(client, stranger, o.org, NAME, user_id=stranger.id)
    assert [r["name"] for r in own["rows"]] == ["Sam Stranger"]


async def test_the_report_needs_report_read(client: AsyncClient, monkeypatch) -> None:
    o = await office(client, monkeypatch)
    await custom_role(o.org, "Time only", {"time.log", "time.read_all"})
    person = await add_member(o.org["id"], "Time only")
    refused = await call(client, person, o.org, NAME)
    assert refused.status_code == 403 and error(refused)["code"] == "PERMISSION_DENIED"
    # The screen itself stays open to them: the report is the extra permission.
    assert isinstance(await missing(client, person, o.org), list)


async def test_another_practice_answers_404(client: AsyncClient, monkeypatch) -> None:
    o = await office(client, monkeypatch)
    other_owner, other = await practice(client, "Other & Co")
    assert (await call(client, other_owner, o.org, NAME)).status_code == 404
    for params in ({"user_id": other_owner.id}, {"manager_user_id": other_owner.id}):
        response = await call(client, o.owner, o.org, NAME, **params)
        assert response.status_code == 404, params
        assert error(response)["code"] == "RESOURCE_NOT_FOUND"
    data = await get_data(client, other_owner, other, NAME)
    assert data["rows"] == [] or all(r["name"] != "Ann Adams" for r in data["rows"])


@pytest.mark.parametrize(
    ("params", "field", "code"),
    [
        ({"week_from": date(2026, 9, 14), "week_to": date(2026, 9, 7)}, "week_from", None),
        ({"week_to": date(2026, 9, 21)}, "week_to", "future_week"),
        ({"week_from": date(2025, 1, 6), "week_to": date(2026, 9, 7)}, "week_to", "range_too_long"),
        ({"status": ["sleepy"]}, "query.status", None),
        ({"week_from": "soon"}, "query.week_from", None),
        ({"user_id": "not-an-id"}, "query.user_id", None),
        ({"limit": 0}, "query.limit", None),
        ({"format": "html"}, "query.format", None),
    ],
)
async def test_validation(
    client: AsyncClient, monkeypatch, params: dict, field: str, code: str | None
) -> None:
    o = await office(client, monkeypatch)
    response = await call(client, o.owner, o.org, NAME, **params)
    assert response.status_code == 422, response.text
    body = error(response)
    assert body["code"] == "VALIDATION_ERROR"
    details = [d for d in body["details"] if d["field"].startswith(field)]
    assert details, body
    if code:
        assert details[0].get("code") == code


async def test_twenty_six_weeks_are_allowed(client: AsyncClient, monkeypatch) -> None:
    o = await office(client, monkeypatch)
    data = await get_data(
        client, o.owner, o.org, NAME, week_from=date(2026, 3, 16), week_to=date(2026, 9, 7)
    )
    assert data["totals"]["weeks"] == 26
