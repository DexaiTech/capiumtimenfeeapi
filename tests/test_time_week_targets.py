"""Targets, working days and missing days on the week grid, and the day and calendar views."""

from datetime import date

from httpx import AsyncClient

from tests.domain_factories import make_client, make_staff_rate, practice, staff
from tests.time_gap_helpers import approve_sheet, day, pin_today, set_member
from tests.time_helpers import err, log_time, ok_data, org_url, set_manager, set_practice

OVER = date(2026, 9, 16)  # a Wednesday: the test week (from Monday 7 September) is over


async def week(client: AsyncClient, actor, org: dict, start: str | None = None, **params):
    params = {**({"week_start": start} if start else {}), **params}
    response = await client.get(
        org_url(org, "/time-entries/week"), headers=actor.headers, params=params
    )
    return ok_data(response)


# --- The week grid --------------------------------------------------------------------------


async def test_the_week_grid_has_targets_working_days_missing_days_and_variance(
    client: AsyncClient, monkeypatch
) -> None:
    pin_today(monkeypatch, OVER)
    _, org = await practice(client)
    person = await staff(org)
    grid = await week(client, person, org, day(0))
    assert grid["today"] == OVER.isoformat()
    # The default member works Monday to Friday, 37.5 hours a week: 7.5 hours a day.
    assert grid["targets"] == {
        "daily_minutes": {**{day(i): 450 for i in range(5)}, day(5): 0, day(6): 0},
        "weekly_minutes": 2250,
    }
    assert grid["working_days"] == [day(i) for i in range(5)]
    assert grid["missing_days"] == [day(i) for i in range(5)]
    assert grid["day_variance"] == [-450] * 5 + [0, 0]

    await log_time(client, person, org, entry_date=day(0), minutes=450)  # on target
    await log_time(client, person, org, entry_date=day(1), minutes=480)  # 30 over
    await log_time(client, person, org, entry_date=day(5), minutes=60)  # a day off
    grid = await week(client, person, org, day(0))
    assert grid["day_variance"] == [0, 30, -450, -450, -450, 60, 0]
    assert grid["missing_days"] == [day(2), day(3), day(4)]
    assert grid["total_minutes"] == 990  # the existing fields are unchanged
    assert grid["working_days"] == [day(i) for i in range(5)]


async def test_the_weekly_capacity_is_spread_over_the_working_days(
    client: AsyncClient, monkeypatch
) -> None:
    pin_today(monkeypatch, OVER)
    _, org = await practice(client)
    person = await staff(org)
    await set_member(org, person, weekly_capacity_minutes=2000, working_days=[5, 1, 3])
    grid = await week(client, person, org, day(0))
    # 2000 / 3 = 666 remainder 2: the first two working days get the extra minute.
    assert grid["targets"]["daily_minutes"] == {
        day(0): 667,
        day(1): 0,
        day(2): 667,
        day(3): 0,
        day(4): 666,
        day(5): 0,
        day(6): 0,
    }
    assert grid["targets"]["weekly_minutes"] == 2000
    assert grid["working_days"] == [day(0), day(2), day(4)]
    assert grid["missing_days"] == [day(0), day(2), day(4)]


async def test_days_outside_the_employment_period_are_not_working_days(
    client: AsyncClient, monkeypatch
) -> None:
    pin_today(monkeypatch, OVER)
    _, org = await practice(client)
    person = await staff(org)
    await set_member(org, person, start_date=date(2026, 9, 9), leaving_date=date(2026, 9, 10))
    grid = await week(client, person, org, day(0))
    assert grid["working_days"] == [day(2), day(3)]
    assert grid["targets"]["daily_minutes"][day(0)] == 0
    assert grid["targets"]["daily_minutes"][day(2)] == 450
    assert grid["targets"]["weekly_minutes"] == 900
    assert grid["missing_days"] == [day(2), day(3)]


async def test_nothing_is_expected_of_someone_without_capacity(
    client: AsyncClient, monkeypatch
) -> None:
    pin_today(monkeypatch, OVER)
    _, org = await practice(client)
    person = await staff(org)
    await set_member(org, person, weekly_capacity_minutes=0)
    grid = await week(client, person, org, day(0))
    assert grid["targets"]["weekly_minutes"] == 0
    assert set(grid["targets"]["daily_minutes"].values()) == {0}
    assert grid["missing_days"] == []
    await log_time(client, person, org, entry_date=day(0), minutes=60)
    assert (await week(client, person, org, day(0)))["day_variance"][0] == 60


async def test_missing_days_run_up_to_today_and_never_into_the_future(
    client: AsyncClient, monkeypatch
) -> None:
    pin_today(monkeypatch, date(2026, 9, 9))  # the Wednesday of the test week
    _, org = await practice(client)
    person = await staff(org)
    assert (await week(client, person, org, day(0)))["missing_days"] == [day(0), day(1), day(2)]
    await log_time(client, person, org, entry_date=day(2), minutes=30)
    assert (await week(client, person, org, day(0)))["missing_days"] == [day(0), day(1)]
    # A week that has not started has no missing days (but has targets).
    future = await week(client, person, org, day(7))
    assert future["missing_days"] == [] and future["targets"]["weekly_minutes"] == 2250


async def test_a_practice_whose_week_starts_on_sunday(client: AsyncClient, monkeypatch) -> None:
    pin_today(monkeypatch, OVER)
    _, org = await practice(client)
    await set_practice(org, week_start=7)
    person = await staff(org)
    grid = await week(client, person, org, "2026-09-09")  # any day of the week
    assert grid["week_start"] == "2026-09-06" and grid["days"][0] == "2026-09-06"
    assert grid["working_days"] == [
        "2026-09-07",
        "2026-09-08",
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
    ]
    assert grid["targets"]["daily_minutes"]["2026-09-06"] == 0
    assert grid["targets"]["weekly_minutes"] == 2250


async def test_the_targets_of_another_person_follow_the_access_rules(
    client: AsyncClient, monkeypatch
) -> None:
    pin_today(monkeypatch, OVER)
    _, org = await practice(client)
    person = await staff(org)
    lead = await staff(org)
    stranger = await staff(org)
    viewer = await staff(org, "viewer")
    await set_manager(org, person, lead)
    await set_member(org, person, weekly_capacity_minutes=1500)
    for actor in (lead, viewer):
        grid = await week(client, actor, org, day(0), user_id=str(person.id))
        assert grid["targets"]["weekly_minutes"] == 1500
    response = await client.get(
        org_url(org, "/time-entries/week"),
        headers=stranger.headers,
        params={"week_start": day(0), "user_id": str(person.id)},
    )
    assert response.status_code == 403


# --- The day view ---------------------------------------------------------------------------


async def test_the_day_view(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    await make_staff_rate(org, person, 12_000, 4_000)
    await log_time(client, person, org, client_id=acme.id, minutes=120, description="Accounts")
    await log_time(client, person, org, minutes=60)
    url = org_url(org, "/time-entries/day")
    view = ok_data(await client.get(url, headers=person.headers, params={"date": day(0)}))
    assert view["date"] == day(0) and view["user_id"] == str(person.id)
    assert view["week_start"] == day(0) and view["is_working_day"] is True
    assert view["timesheet_status"] == "open" and view["timesheet_id"]
    assert (view["total_minutes"], view["billable_minutes"], view["non_billable_minutes"]) == (
        180,
        120,
        60,
    )
    assert (view["target_minutes"], view["remaining_minutes"], view["variance_minutes"]) == (
        450,
        270,
        -270,
    )
    assert len(view["entries"]) == 2 and view["timer"] is None
    assert view["entries"][0]["cost_rate"] is None  # no rate.read
    seen = ok_data(
        await client.get(
            url, headers=owner.headers, params={"date": day(0), "user_id": str(person.id)}
        )
    )
    assert seen["entries"][0]["cost_rate"] == 4_000

    # Over the target: nothing remains. A day off has no target and no entries.
    await log_time(client, person, org, minutes=300)
    over = ok_data(await client.get(url, headers=person.headers, params={"date": day(0)}))
    assert (over["remaining_minutes"], over["variance_minutes"]) == (0, 30)
    off = ok_data(await client.get(url, headers=person.headers, params={"date": day(5)}))
    assert off["is_working_day"] is False and off["target_minutes"] == 0
    assert off["entries"] == [] and off["remaining_minutes"] == 0 and off["total_minutes"] == 0
    assert off["timesheet_id"] == view["timesheet_id"]  # the same week, the same sheet


async def test_the_day_view_shows_the_callers_own_timer_only(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    started = await client.post(org_url(org, "/timer/start"), headers=person.headers, json={})
    assert started.status_code == 201, started.text
    url = org_url(org, "/time-entries/day")
    own = ok_data(await client.get(url, headers=person.headers, params={"date": day(0)}))
    assert own["timer"]["is_running"] is True and own["timer"]["user_id"] == str(person.id)
    others = ok_data(
        await client.get(
            url, headers=owner.headers, params={"date": day(0), "user_id": str(person.id)}
        )
    )
    assert others["timer"] is None
    assert (
        ok_data(await client.get(url, headers=owner.headers, params={"date": day(0)}))["timer"]
        is None
    )


async def test_the_day_and_calendar_views_follow_the_access_rules(client: AsyncClient) -> None:
    _, org = await practice(client)
    _, other = await practice(client, "Other & Co")
    person = await staff(org)
    lead = await staff(org)
    stranger = await staff(org)
    viewer = await staff(org, "viewer")
    outsider = await staff(other)
    await set_manager(org, person, lead)
    await log_time(client, person, org)
    mine = {"user_id": str(person.id)}
    for path, params in (
        ("/time-entries/day", {"date": day(0), **mine}),
        ("/time-entries/calendar", {"from": day(0), "to": day(6), **mine}),
    ):
        for actor, status in ((person, 200), (lead, 200), (viewer, 200), (stranger, 403)):
            response = await client.get(org_url(org, path), headers=actor.headers, params=params)
            assert response.status_code == status, (path, actor)
        # Another practice's member cannot enter this practice at all.
        response = await client.get(org_url(org, path), headers=outsider.headers, params=params)
        assert response.status_code == 404


async def test_day_view_validation(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    url = org_url(org, "/time-entries/day")
    assert (await client.get(url, headers=person.headers)).status_code == 422
    for bad in ({"date": "yesterday"}, {"date": "1800-01-01"}, {"date": day(0), "user_id": "x"}):
        assert (await client.get(url, headers=person.headers, params=bad)).status_code == 422


# --- The calendar view ----------------------------------------------------------------------


async def test_the_calendar_view(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    first = await log_time(client, person, org, entry_date=day(0), client_id=acme.id, minutes=120)
    await log_time(client, person, org, entry_date=day(2), minutes=60)
    await log_time(client, person, org, entry_date=day(7), client_id=acme.id, minutes=30)
    await approve_sheet(client, org, person, owner, first["timesheet_id"])
    calendar = ok_data(
        await client.get(
            org_url(org, "/time-entries/calendar"),
            headers=person.headers,
            params={"from": day(0), "to": day(13)},
        )
    )
    assert calendar["user_id"] == str(person.id)
    assert (calendar["date_from"], calendar["date_to"]) == (day(0), day(13))
    days = calendar["days"]
    assert [d["date"] for d in days] == [day(i) for i in range(14)]
    monday = days[0]
    assert (monday["total_minutes"], monday["billable_minutes"], monday["entry_count"]) == (
        120,
        120,
        1,
    )
    assert (monday["target_minutes"], monday["is_working_day"], monday["week_start"]) == (
        450,
        True,
        day(0),
    )
    assert monday["timesheet_status"] == "approved" and days[6]["timesheet_status"] == "approved"
    wednesday = days[2]
    assert (wednesday["total_minutes"], wednesday["billable_minutes"]) == (60, 0)
    saturday = days[5]
    assert (saturday["target_minutes"], saturday["is_working_day"], saturday["entry_count"]) == (
        0,
        False,
        0,
    )
    next_monday = days[7]
    assert next_monday["total_minutes"] == 30 and next_monday["week_start"] == day(7)
    assert next_monday["timesheet_status"] == "open"
    assert calendar["total_minutes"] == 210 and calendar["billable_minutes"] == 150
    assert calendar["target_minutes"] == 4500


async def test_the_calendar_covers_at_most_62_days(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    url = org_url(org, "/time-entries/calendar")
    ok = await client.get(url, headers=person.headers, params={"from": day(0), "to": day(61)})
    assert len(ok_data(ok)["days"]) == 62
    # The range starts mid-week: the first day still knows its week.
    single = await client.get(url, headers=person.headers, params={"from": day(2), "to": day(2)})
    assert ok_data(single)["days"][0]["week_start"] == day(0)
    long = await client.get(url, headers=person.headers, params={"from": day(0), "to": day(62)})
    error = err(long, 422)
    assert error["details"][0]["field"] == "to" and error["details"][0]["code"] == "range_too_long"
    backwards = await client.get(url, headers=person.headers, params={"from": day(3), "to": day(2)})
    assert err(backwards, 422)["details"][0]["field"] == "to"
    for params in ({}, {"from": day(0)}, {"to": day(0)}, {"from": "x", "to": day(1)}):
        assert (await client.get(url, headers=person.headers, params=params)).status_code == 422
