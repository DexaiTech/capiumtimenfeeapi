"""Overtime capture (APT81, phase B): time beyond a person's standard hours of the week or month is
overtime, the crossing entry is split, a write that adds overtime must be confirmed (409
``OVERTIME_CONFIRMATION_REQUIRED``), edits and deletes re-split the period, locked days are never
rewritten, and charge-out, WIP and invoices never change."""

from datetime import date, timedelta

from httpx import AsyncClient

from tests.billing_helpers import draft
from tests.clients_helpers import sql
from tests.domain_factories import staff
from tests.factories import audit_entries
from tests.overtime_helpers import (
    MONDAY,
    add_overtime_rate,
    is_overtime,
    log,
    overtime_rows,
    scene,
)
from tests.period_lock_helpers import set_lock
from tests.reports_helpers import report
from tests.team_helpers import new_staff
from tests.time_helpers import age_timer, entry_row, err, ok_data, org_url, set_practice, set_sheet

FRIDAY = MONDAY + timedelta(days=4)


async def test_the_ticket_example(client: AsyncClient) -> None:
    """37.5 h a week, £20/h cost, grade rate 1.5x (£30/h): 42 h logged, 3 h of it overtime on Job X
    and 1.5 h on an Internal job. The crossing entry asks first; standard £750, overtime £135 =
    £90 allocated + £45 unallocated, pending; charge-out, WIP and the invoice are unchanged; a
    staff rate then replaces the grade rate."""
    s = await scene(client)
    for offset in range(4):
        entry = await log(client, s, MONDAY + timedelta(days=offset), 480)
        assert entry["is_overtime"] is False

    refused = await log(client, s, FRIDAY, 510, status=409)
    assert refused["code"] == "OVERTIME_CONFIRMATION_REQUIRED"
    assert refused["message"] == "You are adding overtime hours. Are you sure?"
    assert refused["details"] == {
        "overtime_minutes": 180,
        "standard_minutes": 2250,
        "period_start": "2026-09-07",
        "period_end": "2026-09-13",
    }
    assert len(await sql("SELECT id FROM time_entries WHERE user_id = :u", u=str(s.person.id))) == 4

    crossing = await log(client, s, FRIDAY, 510, confirm=True)
    assert crossing["is_overtime"] is True
    # Charge-out never changes: £60 x 8.5 h.
    assert (crossing["charge_rate"], crossing["charge_value"]) == (6_000, 51_000)
    row = await entry_row(crossing["id"])
    assert (row.cost_rate, row.cost_value) == (2_000, 17_000)

    internal = await log(client, s, FRIDAY, 90, s.internal_job, status=409)
    assert internal["details"]["overtime_minutes"] == 90
    internal = await log(client, s, FRIDAY, 90, s.internal_job, confirm=True)

    rows = await overtime_rows(s.org)
    assert set(rows) == {crossing["id"], internal["id"]}
    x, admin = rows[crossing["id"]], rows[internal["id"]]
    assert (x["overtime_minutes"], x["allocated"], x["rate_source"]) == (180, True, "grade")
    assert (x["overtime_rate"], x["overtime_cost"], x["premium_cost"]) == (3_000, 9_000, 3_000)
    assert (admin["overtime_minutes"], admin["allocated"], admin["overtime_cost"]) == (
        90,
        False,
        4_500,
    )
    assert x["status"] == admin["status"] == "pending"
    assert x["confirmed_by"] == s.person.id and x["confirmed_at"] is not None
    confirmed = await audit_entries(s.org["id"], "overtime.confirmed")
    assert [e["metadata"]["overtime_minutes"] for e in confirmed] == [180, 90]

    staff_cost = await report(
        client, s.owner, s.org, "staff-cost", date_from=MONDAY, date_to=MONDAY + timedelta(days=6)
    )
    [line] = staff_cost["rows"]
    assert (line["name"], line["basis"], line["period_start"], line["period_end"]) == (
        "Sam Senior",
        "weekly",
        "2026-09-07",
        "2026-09-13",
    )
    assert (
        {k: line[k] for k in staff_cost["totals"]}
        == staff_cost["totals"]
        == {
            "standard_minutes": 2250,
            "standard_cost": 75_000,
            "overtime_minutes_allocated": 180,
            "overtime_cost_allocated": 9_000,
            "overtime_minutes_unallocated": 90,
            "overtime_cost_unallocated": 4_500,
            "total_cost": 88_500,
            "pending_overtime_cost": 13_500,
        }
    )
    jobs = await report(
        client, s.owner, s.org, "staff-cost/jobs", user_id=s.person.id, date_from=MONDAY
    )
    flagged = {r["job"]["name"]: r for r in jobs["rows"]}
    assert flagged["Job X"]["overtime"] is True
    assert (
        flagged["Job X"]["overtime_minutes"],
        flagged["Job X"]["overtime_rate"],
        flagged["Job X"]["overtime_cost"],
        flagged["Job X"]["status"],
        flagged["Job X"]["allocated"],
        flagged["Job X"]["client"]["name"],
    ) == (180, 3_000, 9_000, "pending", True, "Acme Ltd")
    assert [r["allocated"] for r in jobs["rows"] if r["job"]["name"] != "Job X"] == [False]
    assert jobs["totals"] == {"overtime_minutes": 270, "overtime_cost": 13_500}

    # WIP, the invoice and the cost reports carry the standard values only.
    await set_sheet(crossing["timesheet_id"], status="approved")
    wip = ok_data(await client.get(org_url(s.org, "/wip"), headers=s.owner.headers))
    assert [(w["client"]["name"], w["time_value"]) for w in wip] == [("Acme Ltd", 243_000)]
    ids = [
        e["id"]
        for e in (await sql("SELECT id FROM time_entries WHERE job_id = :j", j=str(s.job_x.id)))
    ]
    invoice = await draft(client, s.owner, s.org, s.acme.id, time=ids)
    assert invoice["net_total"] == 243_000
    cost = await report(
        client,
        s.owner,
        s.org,
        "cost-to-business",
        group_by="person",
        date_from=MONDAY,
        date_to=MONDAY + timedelta(days=6),
    )
    assert cost["totals"]["cost_value"] == 42 * 2_000

    # A staff rate replaces the grade rate (never both): £25 an hour, fixed.
    await add_overtime_rate(
        client, s.owner, s.org, level="staff", user_id=s.person.id, mode="fixed", rate=2_500
    )
    next_monday = MONDAY + timedelta(days=7)
    await log(client, s, next_monday, 1_200)
    later = await log(client, s, next_monday + timedelta(days=1), 1_110, confirm=True)
    mine = (await overtime_rows(s.org))[later["id"]]
    assert (mine["overtime_minutes"], mine["rate_source"], mine["overtime_rate"]) == (
        60,
        "staff",
        2_500,
    )
    assert (mine["overtime_cost"], mine["premium_cost"]) == (2_500, 500)


async def test_edits_and_deletes_re_split_the_period(client: AsyncClient) -> None:
    s = await scene(client, standard_minutes=600)
    monday = await log(client, s, MONDAY, 360)
    tuesday = await log(client, s, MONDAY + timedelta(days=1), 360, confirm=True)
    wednesday = await log(client, s, MONDAY + timedelta(days=2), 60, confirm=True)
    rows = await overtime_rows(s.org)
    assert {k: r["overtime_minutes"] for k, r in rows.items()} == {
        tuesday["id"]: 120,
        wednesday["id"]: 60,
    }
    url = org_url(s.org, f"/time-entries/{monday['id']}")
    # Less overtime: no question asked; the later entries are re-split.
    shorter = await client.patch(url, headers=s.person.headers, json={"minutes": 300})
    assert shorter.status_code == 200, shorter.text
    assert shorter.json()["data"]["is_overtime"] is False
    rows = await overtime_rows(s.org)
    assert {k: r["overtime_minutes"] for k, r in rows.items()} == {
        tuesday["id"]: 60,
        wednesday["id"]: 60,
    }
    # More overtime from an earlier entry: asked, even though the overtime lands later.
    longer = await client.patch(url, headers=s.person.headers, json={"minutes": 420})
    assert err(longer, 409)["details"]["overtime_minutes"] == 120
    longer = await client.patch(
        url, headers=s.person.headers, json={"minutes": 420, "confirm_overtime": True}
    )
    assert longer.status_code == 200, longer.text
    assert {k: r["overtime_minutes"] for k, r in (await overtime_rows(s.org)).items()} == {
        tuesday["id"]: 180,
        wednesday["id"]: 60,
    }
    # A description-only edit leaves the overtime alone.
    plain = await client.patch(url, headers=s.person.headers, json={"description": "Audit"})
    assert plain.status_code == 200, plain.text
    # Deleting Tuesday: Monday + Wednesday are within the hours, nothing is asked.
    gone = await client.delete(
        org_url(s.org, f"/time-entries/{tuesday['id']}"), headers=s.person.headers
    )
    assert gone.status_code == 200, gone.text
    assert await overtime_rows(s.org) == {}
    assert await is_overtime(wednesday["id"]) is False
    # Moving an entry into the next week takes its minutes there.
    moved = await client.patch(
        org_url(s.org, f"/time-entries/{wednesday['id']}"),
        headers=s.person.headers,
        json={"entry_date": (MONDAY + timedelta(days=7)).isoformat()},
    )
    assert moved.status_code == 200, moved.text
    assert await overtime_rows(s.org) == {}


async def test_copy_copy_week_and_the_timer_ask_too(client: AsyncClient) -> None:
    s = await scene(client, standard_minutes=60)
    monday = await log(client, s, MONDAY, 60)
    copy_url = org_url(s.org, f"/time-entries/{monday['id']}/copy")
    tuesday = (MONDAY + timedelta(days=1)).isoformat()
    asked = await client.post(copy_url, headers=s.person.headers, json={"date": tuesday})
    assert err(asked, 409)["code"] == "OVERTIME_CONFIRMATION_REQUIRED"
    copied = await client.post(
        copy_url, headers=s.person.headers, json={"date": tuesday, "confirm_overtime": True}
    )
    assert copied.status_code == 201 and copied.json()["data"]["is_overtime"] is True

    week_url = org_url(s.org, "/time-entries/copy-week")
    body = {
        "from_week_start": MONDAY.isoformat(),
        "to_week_start": (MONDAY + timedelta(days=7)).isoformat(),
    }
    asked = await client.post(week_url, headers=s.person.headers, json=body)
    assert err(asked, 409)["details"]["overtime_minutes"] == 60
    # Refused as a whole: nothing was copied.
    assert len(await sql("SELECT id FROM time_entries WHERE user_id = :u", u=str(s.person.id))) == 2
    done = await client.post(
        week_url, headers=s.person.headers, json={**body, "confirm_overtime": True}
    )
    assert len(ok_data(done)["created"]) == 2
    assert len(await overtime_rows(s.org)) == 2

    # The timer writes today's entry: an hour already logged today, so its time is overtime.
    today = date.fromisoformat(
        ok_data(await client.get(org_url(s.org, "/time-entries/week"), headers=s.person.headers))[
            "today"
        ]
    )
    await log(client, s, today, 60, confirm=True)
    started = await client.post(
        org_url(s.org, "/timer/start"), headers=s.person.headers, json={"job_id": str(s.job_x.id)}
    )
    assert started.status_code == 201, started.text
    await age_timer(s.org, s.person, 1_800)
    stop_url = org_url(s.org, "/timer/stop")
    asked = await client.post(stop_url, headers=s.person.headers, json={})
    assert err(asked, 409)["code"] == "OVERTIME_CONFIRMATION_REQUIRED"
    # The timer is kept until the overtime is confirmed.
    assert (await client.get(org_url(s.org, "/timer"), headers=s.person.headers)).json()["data"]
    stopped = await client.post(stop_url, headers=s.person.headers, json={"confirm_overtime": True})
    assert stopped.status_code == 201, stopped.text
    assert stopped.json()["data"]["is_overtime"] is True


async def test_a_bulk_save_asks_once_for_the_batch(client: AsyncClient) -> None:
    s = await scene(client, standard_minutes=60)
    items = [
        {
            "entry_date": (MONDAY + timedelta(days=i)).isoformat(),
            "minutes": 60,
            "job_id": str(s.job_x.id),
        }
        for i in range(2)
    ]
    url = org_url(s.org, "/time-entries/bulk")
    asked = await client.post(url, headers=s.person.headers, json={"entries": items})
    assert err(asked, 409)["details"]["overtime_minutes"] == 60
    saved = await client.post(
        url, headers=s.person.headers, json={"entries": items, "confirm_overtime": True}
    )
    assert [e["is_overtime"] for e in ok_data(saved, 201)] == [False, True]


async def test_correcting_approved_time_asks_and_re_splits(client: AsyncClient) -> None:
    s = await scene(client, standard_minutes=600)
    first = await log(client, s, MONDAY, 600)
    await set_sheet(first["timesheet_id"], status="approved")
    url = org_url(s.org, f"/time-entries/{first['id']}/correct")
    asked = await client.post(url, headers=s.owner.headers, json={"reason": "Typo", "minutes": 660})
    assert err(asked, 409)["details"]["overtime_minutes"] == 60
    fixed = await client.post(
        url,
        headers=s.owner.headers,
        json={"reason": "Typo", "minutes": 660, "confirm_overtime": True},
    )
    assert fixed.status_code == 200, fixed.text
    row = (await overtime_rows(s.org))[first["id"]]
    # Confirmed by the corrector; the person needs approval, so it is pending.
    assert (row["overtime_minutes"], row["status"], row["confirmed_by"]) == (
        60,
        "pending",
        s.owner.id,
    )
    removed = await client.post(
        org_url(s.org, f"/time-entries/{first['id']}/remove"),
        headers=s.owner.headers,
        json={"reason": "Posted twice"},
    )
    assert removed.status_code == 200, removed.text
    assert await overtime_rows(s.org) == {}


async def test_locked_days_are_never_rewritten(client: AsyncClient) -> None:
    s = await scene(client, standard_minutes=600)
    monday = await log(client, s, MONDAY, 700, confirm=True)
    tuesday = await log(client, s, MONDAY + timedelta(days=1), 60, confirm=True)
    await set_lock(s.org, MONDAY)
    # Monday's overtime is whatever it was when the period was locked.
    await sql(
        "UPDATE time_entry_overtime SET overtime_minutes = 50 WHERE time_entry_id = :i",
        i=monday["id"],
    )
    url = org_url(s.org, f"/time-entries/{tuesday['id']}")
    asked = await client.patch(url, headers=s.person.headers, json={"minutes": 120})
    assert err(asked, 409)["details"]["overtime_minutes"] == 60
    done = await client.patch(
        url, headers=s.person.headers, json={"minutes": 120, "confirm_overtime": True}
    )
    assert done.status_code == 200, done.text
    rows = await overtime_rows(s.org)
    assert (rows[monday["id"]]["overtime_minutes"], rows[tuesday["id"]]["overtime_minutes"]) == (
        50,
        120,
    )


async def test_a_monthly_basis_counts_the_calendar_month(client: AsyncClient) -> None:
    s = await scene(client, basis="monthly", standard_minutes=600)
    await log(client, s, date(2026, 9, 28), 400)
    asked = await log(client, s, date(2026, 9, 30), 300, status=409)
    assert asked["details"] == {
        "overtime_minutes": 100,
        "standard_minutes": 600,
        "period_start": "2026-09-01",
        "period_end": "2026-09-30",
    }
    await log(client, s, date(2026, 9, 30), 300, confirm=True)
    # The same week, but a new month: no overtime.
    october = await log(client, s, date(2026, 10, 1), 300)
    assert october["is_overtime"] is False
    staff_cost = await report(
        client, s.owner, s.org, "staff-cost", date_from="2026-09-01", date_to="2026-10-31"
    )
    assert [
        (r["period_start"], r["basis"], r["standard_minutes"], r["overtime_minutes_allocated"])
        for r in staff_cost["rows"]
    ] == [("2026-09-01", "monthly", 600, 100), ("2026-10-01", "monthly", 300, 0)]


async def test_no_approval_needed_means_approved_overtime(client: AsyncClient) -> None:
    # A partner's time needs no approval (timesheet.exempt).
    s = await scene(client, role="partner", standard_minutes=60)
    await log(client, s, MONDAY, 60)
    over = await log(client, s, MONDAY + timedelta(days=1), 30, confirm=True)
    assert (await overtime_rows(s.org))[over["id"]]["status"] == "approved"
    # A practice that does not approve time: everyone's overtime is approved.
    t = await scene(client, standard_minutes=60)
    await set_practice(t.org, require_timesheet_approval=False)
    await log(client, t, MONDAY, 60)
    theirs = await log(client, t, MONDAY + timedelta(days=1), 30, confirm=True)
    assert (await overtime_rows(t.org))[theirs["id"]]["status"] == "approved"


async def test_with_overtime_off_nothing_is_worked_out_or_asked(client: AsyncClient) -> None:
    s = await scene(client, standard_minutes=60, enabled=False)
    await log(client, s, MONDAY, 600)
    long = await log(client, s, MONDAY + timedelta(days=1), 600)
    assert long["is_overtime"] is False
    assert await overtime_rows(s.org) == {}


async def test_no_overtime_rate_costs_the_overtime_at_nothing(client: AsyncClient) -> None:
    s = await scene(client, standard_minutes=60, grade_bp=None)
    await log(client, s, MONDAY, 60)
    over = await log(client, s, MONDAY + timedelta(days=1), 30, confirm=True)
    row = (await overtime_rows(s.org))[over["id"]]
    assert (row["rate_source"], row["overtime_rate_id"], row["overtime_rate"]) == ("none", None, 0)
    assert (row["overtime_cost"], row["premium_cost"]) == (0, 0)


async def test_new_staff_get_weekly_hours_from_their_capacity(client: AsyncClient) -> None:
    s = await scene(client)
    created = await new_staff(
        client,
        s.owner,
        s.org,
        "Nina New",
        working_minutes_per_day=420,
        working_days=[1, 2, 3, 4, 5],
        start_date="2026-10-01",
    )
    hours = ok_data(
        await client.get(
            org_url(s.org, f"/staff/{created['id']}/hours"),
            headers=s.owner.headers,
        )
    )
    assert [
        (h["basis"], h["standard_minutes"], h["effective_from"], h["effective_to"]) for h in hours
    ] == [("weekly", 2100, "2026-10-01", None)]
    # Members added another way (straight into the database) have none: no overtime for them.
    other = await staff(s.org)
    assert await sql("SELECT 1 FROM staff_hours_bases WHERE user_id = :u", u=str(other.id)) == []
