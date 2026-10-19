"""The Application Testing dashboard: exact numbers on a fixed dataset, the change figures, the
status trend rebuilt from the history, tags, assignees, time zones and ranges."""

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.ticket_dashboard import change_of, resolve_range, share_of
from tests.clients_helpers import count_queries, sql
from tests.factories import add_member, error
from tests.ticket_helpers import (
    DASHBOARD_KEYS,
    add_tag,
    insert_ticket,
    make_ticket,
    new_team,
    tag_id,
    ticket_url,
    utc,
)
from tests.time_helpers import ok_data

STATUSES = ["open", "hold", "in_progress", "reopen", "closed"]
RANGE = {"date_from": "2026-09-01", "date_to": "2026-09-30"}


def dashboard_url(t) -> str:
    return ticket_url(t.org, tail="/dashboard")


async def get_dashboard(client, t, actor=None, **params) -> dict:
    response = await client.get(dashboard_url(t), headers=(actor or t.owner).headers, params=params)
    return ok_data(response)


def kpi(value, previous, kind, percent=None):
    return {"value": value, "previous": previous, "change": {"kind": kind, "percent": percent}}


async def practice_today(zone="Europe/London") -> date:
    rows = await sql(f"SELECT (now() AT TIME ZONE '{zone}')::date AS d")
    return rows[0]["d"]


# --- The fixed dataset ----------------------------------------------------------------------
#
# 16 tickets created in September 2026 (the range), 10 in August (the previous period of the
# same 30 days) and 3 outside both. Times are UTC; the practice is on Europe/London (BST, UTC+1).

# The steps each September ticket took after it was created ``open``, as (status, when).
SEP_STEPS = {
    7: [("hold", utc(2026, 9, 13))],
    8: [("hold", utc(2026, 9, 15))],
    9: [("in_progress", utc(2026, 9, 17))],
    11: [("in_progress", utc(2026, 9, 21))],
    12: [("closed", utc(2026, 9, 23)), ("reopen", utc(2026, 9, 25))],
    13: [("closed", utc(2026, 9, 26))],
    14: [("in_progress", utc(2026, 9, 27)), ("closed", utc(2026, 9, 29))],
    16: [("closed", utc(2026, 9, 30, 18))],
}
SEP_FIRST = {10: "in_progress", 15: "closed"}  # created in that status instead of open
SEP_CREATED = {
    1: utc(2026, 8, 31, 23, 30),  # 00:30 on 1 September in London: the first minutes of the range
    2: utc(2026, 9, 2),
    3: utc(2026, 9, 3),
    4: utc(2026, 9, 5),
    5: utc(2026, 9, 8),
    6: utc(2026, 9, 10),
    7: utc(2026, 9, 12),
    8: utc(2026, 9, 14),
    9: utc(2026, 9, 16),
    10: utc(2026, 9, 18),
    11: utc(2026, 9, 20),
    12: utc(2026, 9, 22),
    13: utc(2026, 9, 24),
    14: utc(2026, 9, 26),
    15: utc(2026, 9, 28),
    16: utc(2026, 9, 30),
}
SEP_PRIORITY = {
    1: "high",
    2: "high",
    3: "medium",
    4: "low",
    5: "not_set",
    6: "not_set",
    7: "high",
    8: "medium",
    9: "low",
    10: "medium",
    11: "not_set",
    12: "high",
    13: "medium",
    14: "low",
    15: "not_set",
    16: "not_set",
}
SEP_ASSIGNEE = {
    1: "alice",
    2: "alice",
    3: "bob",
    6: "carol",
    7: "bob",
    8: "alice",
    9: "carol",
    12: "bob",
    13: "alice",
    14: "carol",
    16: "bob",
}
SEP_TAGS = {
    1: ["Bug", "UI"],
    2: ["Bug"],
    3: ["Bug", "Reporting"],
    4: ["Accounts"],
    5: ["Enhancement"],
    6: ["Docs", "Security"],
    7: ["Validation", "Trial Balance"],  # the only ticket with a tag outside the top seven
    8: ["Reporting"],
    9: ["Bug", "Docs"],
    10: ["UI"],
    11: ["Enhancement"],
    12: ["Bug"],
    13: ["Accounts"],
    14: ["Reporting"],
    15: ["Bug"],
    16: ["Security"],
}
AUG_CREATED = [
    (utc(2026, 8, 1, 23, 30), "open"),  # 00:30 on 2 August in London: the first minutes
    (utc(2026, 8, 5), "open"),
    (utc(2026, 8, 10), "open"),
    (utc(2026, 8, 15), "open"),
    (utc(2026, 8, 18), "in_progress"),
    (utc(2026, 8, 20), "in_progress"),
    (utc(2026, 8, 22), "in_progress"),
    (utc(2026, 8, 25), "reopen"),
    (utc(2026, 8, 28), "reopen"),
    (utc(2026, 8, 31, 12), "closed"),
]
# Outside both periods: a minute before the previous period starts (London), the very instant
# the range ends (exclusive), and long ago.
OUTSIDE = [utc(2026, 8, 1, 22, 59), utc(2026, 9, 30, 23, 0), utc(2026, 7, 15)]


async def build_dataset(client):
    t = await new_team(client)
    org = t.org
    people = {
        "alice": await add_member(org["id"], "staff", full_name="Alice Adams"),
        "bob": await add_member(org["id"], "manager", full_name="Bob Brown"),
        "carol": await add_member(org["id"], "staff", full_name="Carol Clark"),
    }
    await add_tag(org, "Docs", "blue")
    await add_tag(org, "Security", "red")
    for number, created in SEP_CREATED.items():
        first = SEP_FIRST.get(number, "open")
        await insert_ticket(
            org,
            t.owner,
            created_at=created,
            timeline=[(first, created), *SEP_STEPS.get(number, [])],
            priority=SEP_PRIORITY[number],
            number=number,
            subject=f"September {number}",
            assignee=people.get(SEP_ASSIGNEE.get(number, "")),
            tags=SEP_TAGS[number],
        )
    # Everything about the others is "Bug", high and Alice's: any leak into a figure would show.
    for number, (created, status) in enumerate(AUG_CREATED, start=101):
        await insert_ticket(
            org,
            t.owner,
            created_at=created,
            status=status,
            priority="high",
            number=number,
            subject=f"August {number}",
            assignee=people["alice"],
            tags=["Bug"],
        )
    for number, created in enumerate(OUTSIDE, start=201):
        await insert_ticket(
            org,
            t.owner,
            created_at=created,
            status="open",
            priority="high",
            number=number,
            subject=f"Outside {number}",
            assignee=people["alice"],
            tags=["Bug"],
        )
    return t, people


EXPECTED_KPIS = {
    "total": kpi(16, 10, "percent", 60),
    "open": kpi(6, 4, "percent", 50),
    "hold": kpi(2, 0, "new"),
    "in_progress": kpi(3, 3, "percent", 0),
    "reopen": kpi(1, 2, "percent", -50),
    "closed": kpi(4, 1, "percent", 300),
}
# The status each of the 16 tickets had at the end of each of the last 14 days (17-30 September),
# worked out by hand from the steps above: (open, hold, in_progress, reopen, closed).
EXPECTED_TREND = {
    17: (6, 2, 1, 0, 0),
    18: (6, 2, 2, 0, 0),
    19: (6, 2, 2, 0, 0),
    20: (7, 2, 2, 0, 0),
    21: (6, 2, 3, 0, 0),
    22: (7, 2, 3, 0, 0),
    23: (6, 2, 3, 0, 1),
    24: (7, 2, 3, 0, 1),
    25: (7, 2, 3, 1, 0),
    26: (7, 2, 3, 1, 1),
    27: (6, 2, 4, 1, 1),
    28: (6, 2, 4, 1, 2),
    29: (6, 2, 3, 1, 3),
    30: (6, 2, 3, 1, 4),
}


async def test_the_whole_dashboard_on_the_fixed_dataset(client):
    t, people = await build_dataset(client)
    response = await client.get(dashboard_url(t), headers=t.owner.headers, params=RANGE)
    data = ok_data(response)
    assert set(data) == DASHBOARD_KEYS

    # The range and the period before it.
    assert (data["date_from"], data["date_to"], data["days"]) == ("2026-09-01", "2026-09-30", 30)
    assert (data["previous_from"], data["previous_to"]) == ("2026-08-02", "2026-08-31")

    # The six cards: value, the previous period's value and the change.
    assert data["kpis"] == EXPECTED_KPIS

    # Tickets by status: counts and the share to one decimal, half up (18.75 -> 18.8, 6.25 -> 6.3).
    assert data["by_status"] == [
        {"status": "open", "count": 6, "percent": 37.5},
        {"status": "hold", "count": 2, "percent": 12.5},
        {"status": "in_progress", "count": 3, "percent": 18.8},
        {"status": "reopen", "count": 1, "percent": 6.3},
        {"status": "closed", "count": 4, "percent": 25.0},
    ]

    # By priority: always the four, in the order of the bars.
    assert data["by_priority"] == [
        {"priority": "high", "count": 4, "percent": 25.0},
        {"priority": "medium", "count": 4, "percent": 25.0},
        {"priority": "low", "count": 3, "percent": 18.8},
        {"priority": "not_set", "count": 5, "percent": 31.3},
    ]

    # By tag: the seven most used (ties by name), then Others: the tickets with any other tag,
    # each once (ticket 7 has two of them: Trial Balance and Validation).
    colours = {
        "Bug": "red",
        "Reporting": "green",
        "Accounts": "blue",
        "Docs": "blue",
        "Enhancement": "violet",
        "Security": "red",
        "UI": "blue",
    }
    counts = [("Bug", 6), ("Reporting", 3), ("Accounts", 2), ("Docs", 2)]
    counts += [("Enhancement", 2), ("Security", 2), ("UI", 2)]
    expected_tags = [
        {
            "tag_id": await tag_id(t.org, name),
            "name": name,
            "color": colours[name],
            "count": count,
            "is_other": False,
        }
        for name, count in counts
    ]
    others = {"tag_id": None, "name": "Others", "color": None, "count": 1, "is_other": True}
    assert data["by_tag"] == [*expected_tags, others]

    # Top assignees, busiest first (ties by name), the unassigned tickets as a row of their own.
    assert data["top_assignees"] == [
        {"user_id": None, "full_name": "Unassigned", "count": 5},
        {"user_id": str(people["alice"].id), "full_name": "Alice Adams", "count": 4},
        {"user_id": str(people["bob"].id), "full_name": "Bob Brown", "count": 4},
        {"user_id": str(people["carol"].id), "full_name": "Carol Clark", "count": 3},
    ]

    # The seven newest tickets of the range.
    assert [item["number"] for item in data["recent"]] == [16, 15, 14, 13, 12, 11, 10]
    assert [item["subject"] for item in data["recent"]][:2] == ["September 16", "September 15"]

    # The status trend: the last 14 days, each ticket in the status it had at the end of the day.
    assert [point["date"] for point in data["trend"]] == [f"2026-09-{d}" for d in range(17, 31)]
    for point in data["trend"]:
        expected = dict(zip(STATUSES, EXPECTED_TREND[int(point["date"][-2:])], strict=True))
        assert {s: point[s] for s in STATUSES} == expected, point["date"]
        assert point["total"] == sum(expected.values())
    # On the last day every ticket of the range is in its current status.
    last = data["trend"][-1]
    assert {s: last[s] for s in STATUSES} == {
        row["status"]: row["count"] for row in data["by_status"]
    }
    assert last["total"] == data["kpis"]["total"]["value"] == 16


async def test_the_recent_tickets_are_list_items(client):
    t, _ = await build_dataset(client)
    data = await get_dashboard(client, t, **RANGE)
    listed = ok_data(
        await client.get(
            ticket_url(t.org),
            headers=t.owner.headers,
            params={"created_from": "2026-09-01", "created_to": "2026-09-30", "limit": 7},
        )
    )
    assert data["recent"] == listed
    first = data["recent"][0]
    assert first["status"] == "closed" and first["priority"] == "not_set"
    assert [tag["name"] for tag in first["tags"]] == ["Security"]
    assert first["assignee"]["full_name"] == "Bob Brown"


async def test_a_single_day_is_compared_with_the_day_before(client):
    t, _ = await build_dataset(client)
    # Ticket 9 was created at 12:00 UTC on 16 September; nothing was created on the 15th.
    data = await get_dashboard(client, t, date_from="2026-09-16", date_to="2026-09-16")
    assert data["days"] == 1
    assert (data["previous_from"], data["previous_to"]) == ("2026-09-15", "2026-09-15")
    assert data["kpis"]["total"] == kpi(1, 0, "new")
    assert data["kpis"]["in_progress"] == kpi(1, 0, "new")
    assert data["kpis"]["open"] == kpi(0, 0, "none")
    assert [item["number"] for item in data["recent"]] == [9]
    assert len(data["trend"]) == 14 and data["trend"][-1]["date"] == "2026-09-16"
    assert data["trend"][-2]["total"] == 0
    # The cards show the tickets' current status, the trend the status at the end of each day:
    # ticket 9 became "in progress" at 12:00 UTC on the 17th, so on the 16th it was still open.
    assert data["trend"][-1]["open"] == 1 and data["trend"][-1]["in_progress"] == 0


# --- The change -----------------------------------------------------------------------------


def test_change_is_new_none_or_a_whole_percent_rounded_half_away_from_zero():
    def change(current, previous):
        return change_of(current, previous).model_dump()

    assert change(5, 0) == {"kind": "new", "percent": None}
    assert change(0, 0) == {"kind": "none", "percent": None}
    assert change(10, 10) == {"kind": "percent", "percent": 0}
    assert change(0, 5) == {"kind": "percent", "percent": -100}
    assert change(6, 4) == {"kind": "percent", "percent": 50}
    assert change(9, 8) == {"kind": "percent", "percent": 13}  # +12.5
    assert change(5, 8) == {"kind": "percent", "percent": -38}  # -37.5
    assert change(1, 3) == {"kind": "percent", "percent": -67}  # -66.67
    assert change(2, 3) == {"kind": "percent", "percent": -33}  # -33.33
    assert change(4, 1) == {"kind": "percent", "percent": 300}
    assert change(17, 16) == {"kind": "percent", "percent": 6}  # +6.25
    assert change(1, 12) == {"kind": "percent", "percent": -92}  # -91.67


def test_share_is_a_percentage_to_one_decimal_half_up():
    assert share_of(0, 0) is None and share_of(5, 0) is None
    assert share_of(0, 7) == 0.0
    assert share_of(7, 7) == 100.0
    assert share_of(1, 3) == 33.3 and share_of(2, 3) == 66.7
    assert share_of(3, 16) == 18.8  # 18.75
    assert share_of(1, 16) == 6.3  # 6.25
    assert share_of(1, 8) == 12.5
    assert share_of(1, 1000) == 0.1 and share_of(1, 3000) == 0.0


async def test_every_kind_of_change_comes_through_the_api(client):
    t = await new_team(client)
    # Previous period (2026-08-02 to 08-31): two open and a hold.
    # The range: three open, two closed and a reopen.
    tickets = [
        ((8, 5), "open"),
        ((8, 6), "open"),
        ((8, 7), "hold"),
        ((9, 2), "open"),
        ((9, 3), "open"),
        ((9, 4), "open"),
        ((9, 5), "closed"),
        ((9, 6), "closed"),
        ((9, 7), "reopen"),
    ]
    for number, (day, status) in enumerate(tickets, start=1):
        created = utc(2026, *day)
        await insert_ticket(t.org, t.owner, created_at=created, status=status, number=number)
    data = await get_dashboard(client, t, **RANGE)
    assert data["kpis"] == {
        "total": kpi(6, 3, "percent", 100),
        "open": kpi(3, 2, "percent", 50),
        "hold": kpi(0, 1, "percent", -100),
        "in_progress": kpi(0, 0, "none"),
        "reopen": kpi(1, 0, "new"),
        "closed": kpi(2, 0, "new"),
    }


async def test_an_empty_range_is_all_zeros_and_nulls(client):
    t = await new_team(client)
    await make_ticket(client, t.owner, t.org, "Created today, outside the range")
    data = await get_dashboard(client, t, date_from="2020-01-01", date_to="2020-01-31")
    assert set(data) == DASHBOARD_KEYS and data["days"] == 31
    assert data["kpis"] == {name: kpi(0, 0, "none") for name in ("total", *STATUSES)}
    assert data["by_status"] == [{"status": s, "count": 0, "percent": None} for s in STATUSES]
    assert [(p["priority"], p["count"], p["percent"]) for p in data["by_priority"]] == [
        ("high", 0, None),
        ("medium", 0, None),
        ("low", 0, None),
        ("not_set", 0, None),
    ]
    assert data["by_tag"] == [] and data["top_assignees"] == [] and data["recent"] == []
    assert len(data["trend"]) == 14
    assert all(p[s] == 0 for p in data["trend"] for s in (*STATUSES, "total"))
    assert data["trend"][-1]["date"] == "2020-01-31"


# --- Ranges ---------------------------------------------------------------------------------


async def test_without_a_range_it_is_the_last_19_days_ending_today(client):
    t = await new_team(client)
    today = await practice_today()
    data = await get_dashboard(client, t)
    assert data["date_to"] == today.isoformat() and data["days"] == 19
    assert date.fromisoformat(data["date_from"]) == today - timedelta(days=18)
    assert date.fromisoformat(data["previous_to"]) == today - timedelta(days=19)
    assert date.fromisoformat(data["previous_from"]) == today - timedelta(days=37)
    assert len(data["trend"]) == 14 and data["trend"][-1]["date"] == data["date_to"]
    assert data["trend"][0]["date"] == (today - timedelta(days=13)).isoformat()


async def test_a_range_with_only_an_end_looks_back_19_days(client):
    t = await new_team(client)
    data = await get_dashboard(client, t, date_to="2026-03-31")
    assert (data["date_from"], data["date_to"], data["days"]) == ("2026-03-13", "2026-03-31", 19)
    assert data["trend"][0]["date"] == "2026-03-18" and data["trend"][-1]["date"] == "2026-03-31"


async def test_a_range_with_only_a_start_runs_to_today(client):
    t = await new_team(client)
    today = await practice_today()
    data = await get_dashboard(client, t, date_from=(today - timedelta(days=3)).isoformat())
    assert data["date_to"] == today.isoformat() and data["days"] == 4


def test_resolve_range():
    today = date(2026, 10, 18)
    assert resolve_range(None, None, today) == (date(2026, 9, 30), today)
    assert resolve_range(date(2026, 10, 1), None, today) == (date(2026, 10, 1), today)
    assert resolve_range(None, date(2026, 3, 1), today) == (date(2026, 2, 11), date(2026, 3, 1))
    assert resolve_range(date(2026, 5, 5), date(2026, 5, 5), today) == (date(2026, 5, 5),) * 2
    start = date(2025, 1, 1)
    assert resolve_range(start, start + timedelta(days=365), today)[0] == start  # 366 days


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"date_from": "2026-09-30", "date_to": "2026-09-01"}, "date_from"),
        ({"date_from": "2025-01-01", "date_to": "2026-01-02"}, "date_to"),  # 367 days
        ({"date_from": "2026-13-01"}, "query.date_from"),
        ({"date_to": "yesterday"}, "query.date_to"),
    ],
)
async def test_range_validation(client, params, field):
    t = await new_team(client)
    response = await client.get(dashboard_url(t), headers=t.owner.headers, params=params)
    assert response.status_code == 422, response.text
    assert error(response)["details"][0]["field"] == field


async def test_the_longest_range_is_366_days(client):
    t = await new_team(client)
    data = await get_dashboard(client, t, date_from="2025-01-01", date_to="2026-01-01")
    assert data["days"] == 366


# --- Time zones -----------------------------------------------------------------------------


async def test_days_are_the_practices_calendar_days(client):
    t, _ = await build_dataset(client)
    london = await get_dashboard(client, t, **RANGE)
    assert london["kpis"] == EXPECTED_KPIS
    assert next(i["number"] for i in london["recent"]) == 16

    # Auckland is UTC+12 (UTC+13 from 27 September). There the range is 12:00 UTC on 31 August
    # to 11:00 UTC on 30 September, and the previous period starts at 12:00 UTC on 1 August:
    # * "Outside 201" (22:59 UTC on 1 August) is already 2 August: it joins the previous period;
    # * "August 110" (12:00 UTC on 31 August) is the first instant of 1 September: it moves into
    #   the range;
    # * ticket 16 (12:00 UTC on 30 September) is already 1 October: it leaves the range.
    await sql("UPDATE organizations SET timezone = 'Pacific/Auckland' WHERE id = :o", o=t.id)
    auckland = await get_dashboard(client, t, **RANGE)
    assert auckland["kpis"] == {
        "total": kpi(16, 10, "percent", 60),
        "open": kpi(6, 5, "percent", 20),
        "hold": kpi(2, 0, "new"),
        "in_progress": kpi(3, 3, "percent", 0),
        "reopen": kpi(1, 2, "percent", -50),
        "closed": kpi(4, 0, "new"),
    }
    assert [i["number"] for i in auckland["recent"]] == [15, 14, 13, 12, 11, 10, 9]


async def test_the_trend_changes_day_at_the_practices_midnight(client):
    t = await new_team(client)
    # London in September is UTC+1: its midnight is 23:00 UTC.
    # a: created at 22:59 UTC on the 10th (23:59 there), closed at 23:00 UTC (00:00 on the 11th).
    await insert_ticket(
        t.org,
        t.owner,
        created_at=utc(2026, 9, 10, 22, 59),
        timeline=[("open", utc(2026, 9, 10, 22, 59)), ("closed", utc(2026, 9, 10, 23, 0))],
        number=1,
    )
    # b: created at exactly 23:00 UTC on the 10th: it exists from the 11th.
    await insert_ticket(t.org, t.owner, created_at=utc(2026, 9, 10, 23, 0), number=2)
    # c: moved through three statuses within the 12th: only the last one counts.
    await insert_ticket(
        t.org,
        t.owner,
        created_at=utc(2026, 9, 12, 8),
        timeline=[
            ("open", utc(2026, 9, 12, 8)),
            ("hold", utc(2026, 9, 12, 9)),
            ("in_progress", utc(2026, 9, 12, 10)),
        ],
        number=3,
    )
    data = await get_dashboard(client, t, date_from="2026-09-01", date_to="2026-09-14")
    by_day = {p["date"]: tuple(p[s] for s in STATUSES) for p in data["trend"]}
    assert data["trend"][0]["date"] == "2026-09-01" and len(by_day) == 14
    assert by_day["2026-09-09"] == (0, 0, 0, 0, 0)
    assert by_day["2026-09-10"] == (1, 0, 0, 0, 0)  # a is still open at the end of the 10th
    assert by_day["2026-09-11"] == (1, 0, 0, 0, 1)  # a closed at midnight; b exists
    assert by_day["2026-09-12"] == (1, 0, 1, 0, 1)
    assert by_day["2026-09-14"] == (1, 0, 1, 0, 1)
    assert data["trend"][-1]["total"] == 3


async def test_a_ticket_that_was_closed_and_reopened_shows_both_in_the_trend(client):
    t = await new_team(client)
    await insert_ticket(
        t.org,
        t.owner,
        created_at=utc(2026, 9, 2),
        timeline=[
            ("open", utc(2026, 9, 2)),
            ("closed", utc(2026, 9, 4)),
            ("reopen", utc(2026, 9, 6)),
            ("closed", utc(2026, 9, 8)),
        ],
        number=1,
    )
    data = await get_dashboard(client, t, date_from="2026-09-01", date_to="2026-09-14")
    by_day = {p["date"][-2:]: [s for s in STATUSES if p[s]] for p in data["trend"]}
    assert by_day["01"] == [] and by_day["02"] == ["open"] and by_day["03"] == ["open"]
    assert by_day["04"] == ["closed"] and by_day["05"] == ["closed"]
    assert by_day["06"] == ["reopen"] and by_day["07"] == ["reopen"]
    assert by_day["08"] == ["closed"] and by_day["14"] == ["closed"]
    assert data["by_status"][4]["count"] == 1  # closed now
    # Only tickets created in the range are in it: the same ticket seen from October.
    later = await get_dashboard(client, t, date_from="2026-10-01", date_to="2026-10-14")
    assert all(p["total"] == 0 for p in later["trend"]) and later["kpis"]["total"]["value"] == 0


async def test_the_trend_ends_where_the_figures_stand_when_the_range_ends_today(client):
    t = await new_team(client)
    staff = await t.member("staff")
    a = await make_ticket(client, t.owner, t.org, "A")
    await make_ticket(client, t.owner, t.org, "B", status="hold")
    c = await make_ticket(client, staff, t.org, "C")
    d = await make_ticket(client, staff, t.org, "D", priority="high")
    moves = [
        (a["id"], "closed"),
        (c["id"], "in_progress"),
        (d["id"], "closed"),
        (d["id"], "reopen"),
    ]
    for ticket_id, status in moves:
        patched = await client.patch(
            ticket_url(t.org, ticket_id), headers=t.owner.headers, json={"status": status}
        )
        assert patched.status_code == 200

    data = await get_dashboard(client, t)  # the default range ends today
    counts = {row["status"]: row["count"] for row in data["by_status"]}
    assert counts == {"open": 0, "hold": 1, "in_progress": 1, "reopen": 1, "closed": 1}
    last = data["trend"][-1]
    assert {s: last[s] for s in STATUSES} == counts and last["total"] == 4
    assert data["kpis"]["total"] == kpi(4, 0, "new")
    assert [data["kpis"][s]["value"] for s in STATUSES] == [0, 1, 1, 1, 1]
    # Nothing existed on the days before today.
    assert all(p["total"] == 0 for p in data["trend"][:-1])
    assert [row["count"] for row in data["by_priority"]] == [1, 0, 0, 3]


# --- Tags and assignees ---------------------------------------------------------------------


async def test_with_seven_tags_or_fewer_there_is_no_others_bucket(client):
    t = await new_team(client)
    names = ["Bug", "Enhancement", "Accounts", "Reporting", "UI", "Trial Balance", "Validation"]
    for number, name in enumerate(names, start=1):
        await insert_ticket(t.org, t.owner, created_at=utc(2026, 9, 2), number=number, tags=[name])
    data = await get_dashboard(client, t, **RANGE)
    assert [row["name"] for row in data["by_tag"]] == sorted(names, key=str.lower)
    assert not any(row["is_other"] for row in data["by_tag"])
    # An eighth tag makes the least used one fall into Others.
    await add_tag(t.org, "Eighth")
    await insert_ticket(
        t.org, t.owner, created_at=utc(2026, 9, 3), number=9, tags=["Eighth", "Bug"]
    )
    data = await get_dashboard(client, t, **RANGE)
    assert len(data["by_tag"]) == 8
    assert data["by_tag"][0]["name"] == "Bug" and data["by_tag"][0]["count"] == 2
    assert "Validation" not in [row["name"] for row in data["by_tag"]]
    assert data["by_tag"][-1] == {
        "tag_id": None,
        "name": "Others",
        "color": None,
        "count": 1,
        "is_other": True,
    }


async def test_a_ticket_with_several_other_tags_is_counted_once_in_others(client):
    t = await new_team(client)
    for name in ("Seven", "Eight", "Nine", "Ten"):
        await add_tag(t.org, name)
    top = ["Bug", "Enhancement", "Accounts", "Reporting", "UI", "Trial Balance", "Validation"]
    number = 0
    for name in top:  # three tickets each, so these seven outrank the rare tags
        for _ in range(3):
            number += 1
            await insert_ticket(
                t.org, t.owner, created_at=utc(2026, 9, 2), number=number, tags=[name]
            )
    # One ticket with all four rare tags, one with two of them, and a ninth Bug.
    rare = [(50, ["Seven", "Eight", "Nine", "Ten"]), (51, ["Seven", "Eight"]), (52, ["Bug"])]
    for number, tags in rare:
        await insert_ticket(t.org, t.owner, created_at=utc(2026, 9, 3), number=number, tags=tags)
    data = await get_dashboard(client, t, **RANGE)
    assert [row["name"] for row in data["by_tag"]][:-1] == ["Bug", *sorted(top[1:], key=str.lower)]
    assert [row["count"] for row in data["by_tag"]][:-1] == [4, 3, 3, 3, 3, 3, 3]
    others = data["by_tag"][-1]
    assert others == {"tag_id": None, "name": "Others", "color": None, "count": 2, "is_other": True}


async def test_equal_tags_are_ordered_by_name_ignoring_case(client):
    t = await new_team(client)
    await add_tag(t.org, "alpha")
    await add_tag(t.org, "Beta")
    await insert_ticket(
        t.org, t.owner, created_at=utc(2026, 9, 2), number=1, tags=["Beta", "alpha", "UI"]
    )
    data = await get_dashboard(client, t, **RANGE)
    assert [row["name"] for row in data["by_tag"]] == ["alpha", "Beta", "UI"]


async def test_the_top_assignees_are_ten_people_and_the_unassigned_row(client):
    t = await new_team(client)
    people = [
        await add_member(t.org["id"], "staff", full_name=f"Person {i:02d}") for i in range(12)
    ]
    number = 0
    for index, person in enumerate(people):
        for _ in range(12 - index):  # Person 00 has 12 tickets, Person 11 has 1
            number += 1
            await insert_ticket(
                t.org, t.owner, created_at=utc(2026, 9, 2), number=number, assignee=person
            )
    for _ in range(5):
        number += 1
        await insert_ticket(t.org, t.owner, created_at=utc(2026, 9, 2), number=number)
    data = await get_dashboard(client, t, **RANGE)
    # Ten people, the Unassigned row where its count puts it (ties by name), nobody else.
    assert [(r["full_name"], r["count"]) for r in data["top_assignees"]] == [
        ("Person 00", 12),
        ("Person 01", 11),
        ("Person 02", 10),
        ("Person 03", 9),
        ("Person 04", 8),
        ("Person 05", 7),
        ("Person 06", 6),
        ("Person 07", 5),
        ("Unassigned", 5),
        ("Person 08", 4),
        ("Person 09", 3),
    ]
    unassigned = [r for r in data["top_assignees"] if r["full_name"] == "Unassigned"]
    assert unassigned[0]["user_id"] is None


async def test_no_unassigned_row_when_everything_is_assigned(client):
    t = await new_team(client)
    staff = await add_member(t.org["id"], "staff", full_name="Solo Worker")
    await insert_ticket(t.org, t.owner, created_at=utc(2026, 9, 2), number=1, assignee=staff)
    data = await get_dashboard(client, t, **RANGE)
    assert [r["full_name"] for r in data["top_assignees"]] == ["Solo Worker"]


async def test_someone_who_has_left_still_has_their_name(client):
    t = await new_team(client)
    staff = await add_member(t.org["id"], "staff", full_name="Former Colleague")
    await insert_ticket(t.org, t.owner, created_at=utc(2026, 9, 2), number=1, assignee=staff)
    await sql("UPDATE organization_members SET status = 'left' WHERE user_id = :u", u=staff.id)
    data = await get_dashboard(client, t, **RANGE)
    assert data["top_assignees"] == [
        {"user_id": str(staff.id), "full_name": "Former Colleague", "count": 1}
    ]


async def test_only_seven_recent_tickets_and_only_of_the_range(client):
    t = await new_team(client)
    for number in range(1, 11):
        await insert_ticket(t.org, t.owner, created_at=utc(2026, 9, number), number=number)
    await insert_ticket(t.org, t.owner, created_at=utc(2026, 10, 5), number=99)
    data = await get_dashboard(client, t, **RANGE)
    assert [item["number"] for item in data["recent"]] == [10, 9, 8, 7, 6, 5, 4]
    assert data["kpis"]["total"]["value"] == 10


# --- Tenancy, permissions and cost ----------------------------------------------------------


async def test_other_practices_tickets_are_not_counted_and_their_dashboard_is_not_found(client):
    mine = await new_team(client)
    theirs = await new_team(client, "Their Practice")
    await insert_ticket(mine.org, mine.owner, created_at=utc(2026, 9, 2), number=1, tags=["Bug"])
    for number in range(1, 6):
        await insert_ticket(
            theirs.org,
            theirs.owner,
            created_at=utc(2026, 9, 2),
            number=number,
            tags=["Bug"],
            priority="high",
        )
    data = await get_dashboard(client, mine, **RANGE)
    assert data["kpis"]["total"]["value"] == 1
    assert data["by_tag"][0]["count"] == 1 and data["by_priority"][0]["count"] == 0
    assert len(data["recent"]) == 1
    response = await client.get(dashboard_url(theirs), headers=mine.owner.headers, params=RANGE)
    assert response.status_code == 404


@pytest.mark.parametrize(
    "role", ["owner", "admin", "partner", "manager", "staff", "billing", "viewer"]
)
async def test_every_system_role_can_see_the_dashboard(client, role):
    t = await new_team(client)
    actor = await t.member(role)
    response = await client.get(dashboard_url(t), headers=actor.headers, params=RANGE)
    assert response.status_code == 200


async def test_the_dashboard_costs_a_bounded_number_of_queries(client):
    t, _ = await build_dataset(client)
    with count_queries() as statements:
        data = await get_dashboard(client, t, **RANGE)
    assert data["kpis"]["total"]["value"] == 16
    assert len(statements) <= 16, len(statements)


def test_london_summer_time_is_what_the_datasets_rely_on():
    assert utc(2026, 9, 1).astimezone(ZoneInfo("Europe/London")).utcoffset() == timedelta(hours=1)
    assert utc(2026, 1, 1).astimezone(ZoneInfo("Europe/London")).utcoffset() == timedelta(0)
