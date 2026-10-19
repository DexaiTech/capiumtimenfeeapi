"""Missing timesheets, reminders (once per person and week) and the daily reminder job."""

import asyncio
import uuid
from datetime import date

from httpx import AsyncClient

from app.db.session import get_sessionmaker
from app.services import jobs as job_queue
from app.services import timesheet_reminders as reminders_service
from tests.clients_helpers import sql
from tests.conftest import run_jobs
from tests.domain_factories import practice, staff
from tests.factories import audit_entries
from tests.notification_helpers import inbox, put_practice
from tests.time_gap_helpers import (
    day,
    missing,
    pin_today,
    reminder_rows,
    run_reminders,
    set_member,
    sheet_action,
)
from tests.time_helpers import log_time, org_url, set_manager

# A Wednesday: the week of Monday 7 September is the last completed week.
OVER = date(2026, 9, 16)


async def firm(client: AsyncClient):
    """A practice whose owner is not asked for a timesheet."""
    owner, org = await practice(client)
    await set_member(org, owner, weekly_capacity_minutes=0)
    return owner, org


async def submitted(
    client: AsyncClient, org: dict, person, *, entry_date: str | None = None
) -> str:
    """The person logs an hour and submits the week; returns the timesheet id."""
    entry = await log_time(client, person, org, entry_date=entry_date or day(0))
    await sheet_action(client, person, org, entry["timesheet_id"], "submit")
    return str(entry["timesheet_id"])


def names(rows: list[dict]) -> list[str]:
    return [row["name"] for row in rows]


# --- The list -------------------------------------------------------------------------------


async def test_missing_timesheets_of_the_last_completed_week(
    client: AsyncClient, monkeypatch
) -> None:
    pin_today(monkeypatch, OVER)
    owner, org = await firm(client)
    ann = await staff(org, full_name="Ann Adams")  # nothing at all
    bob = await staff(org, full_name="Bob Brown")  # logged, not submitted
    cat = await staff(org, full_name="Cat Clark")  # submitted
    dan = await staff(org, full_name="Dan Dale")  # returned
    eve = await staff(org, full_name="Eve East")  # approved
    jan = await staff(org, full_name="Jan Jones")  # started on the Wednesday
    await set_member(org, jan, start_date=date(2026, 9, 9))
    await log_time(client, bob, org, minutes=60)
    await submitted(client, org, cat)
    returned = await submitted(client, org, dan)
    await sheet_action(client, owner, org, returned, "reject", json={"decision_note": "Detail"})
    approved = await submitted(client, org, eve)
    await sheet_action(client, owner, org, approved, "approve")

    rows = await missing(client, owner, org)
    assert names(rows) == ["Ann Adams", "Bob Brown", "Dan Dale", "Jan Jones"]
    by_name = {row["name"]: row for row in rows}
    assert by_name["Ann Adams"] == {
        "user_id": str(ann.id),
        "name": "Ann Adams",
        "week_start": day(0),
        "status": "none",
        "timesheet_id": None,
        "logged_minutes": 0,
        "expected_minutes": 2250,
        "missing_days": [day(i) for i in range(5)],
        "last_reminded_at": None,
        "has_login": True,
    }
    bob_row = by_name["Bob Brown"]
    assert bob_row["status"] == "open" and bob_row["timesheet_id"]
    assert (bob_row["logged_minutes"], bob_row["expected_minutes"]) == (60, 2250)
    assert bob_row["missing_days"] == [day(i) for i in range(1, 5)]  # Monday has time
    assert by_name["Dan Dale"]["status"] == "rejected"
    jan_row = by_name["Jan Jones"]
    assert jan_row["expected_minutes"] == 1350 and jan_row["missing_days"] == [
        day(2),
        day(3),
        day(4),
    ]
    # Any day of the week names the week.
    assert await missing(client, owner, org, week_start=day(3)) == rows


async def test_who_is_expected_to_record_time(client: AsyncClient, monkeypatch) -> None:
    pin_today(monkeypatch, OVER)
    owner, org = await firm(client)
    await staff(org, full_name="Ann Adams")
    unpaid = await staff(org, full_name="No Capacity")
    paused = await staff(org, full_name="Paused Person")
    later = await staff(org, full_name="Starts Next Week")
    gone = await staff(org, full_name="Left Already")
    leaving = await staff(org, full_name="Leaves On Tuesday")
    await set_member(org, unpaid, weekly_capacity_minutes=0)
    await set_member(org, paused, status="inactive")
    await set_member(org, later, start_date=date(2026, 9, 14))
    await set_member(org, gone, leaving_date=date(2026, 9, 4))
    await set_member(org, leaving, leaving_date=date(2026, 9, 8))
    week_one = {row["name"]: row for row in await missing(client, owner, org)}
    assert set(week_one) == {"Ann Adams", "Leaves On Tuesday"}
    assert week_one["Leaves On Tuesday"]["expected_minutes"] == 900
    assert week_one["Leaves On Tuesday"]["missing_days"] == [day(0), day(1)]
    # The next week: the person who starts is expected, and today (Wednesday) counts.
    week_two = {row["name"]: row for row in await missing(client, owner, org, week_start=day(7))}
    assert set(week_two) == {"Ann Adams", "Starts Next Week"}
    assert week_two["Ann Adams"]["missing_days"] == [day(7), day(8), day(9)]
    # A week that has not started has no missing days.
    week_three = await missing(client, owner, org, week_start=day(14))
    assert {row["name"]: row["missing_days"] for row in week_three} == {
        "Ann Adams": [],
        "Starts Next Week": [],
    }


async def test_missing_timesheets_access_and_filters(client: AsyncClient, monkeypatch) -> None:
    pin_today(monkeypatch, OVER)
    owner, org = await firm(client)
    _, other = await practice(client, "Other & Co")
    outsider = await staff(other)
    ann = await staff(org, full_name="Ann Adams")
    bob = await staff(org, full_name="Bob Brown")
    dan = await staff(org, full_name="Dan Dale")
    lead = await staff(org, full_name="Lena Lead")
    stranger = await staff(org, full_name="Sam Stranger")
    viewer = await staff(org, "viewer", full_name="Vic Viewer")
    for member in (lead, stranger, viewer):
        await set_member(org, member, weekly_capacity_minutes=0)
    await set_manager(org, ann, lead)
    await set_manager(org, bob, lead)

    assert names(await missing(client, owner, org)) == ["Ann Adams", "Bob Brown", "Dan Dale"]
    assert names(await missing(client, viewer, org)) == ["Ann Adams", "Bob Brown", "Dan Dale"]
    assert names(await missing(client, owner, org, user_id=ann.id)) == ["Ann Adams"]
    assert names(await missing(client, owner, org, manager_user_id=lead.id)) == [
        "Ann Adams",
        "Bob Brown",
    ]
    assert names(await missing(client, owner, org, user_id=ann.id, manager_user_id=lead.id)) == [
        "Ann Adams"
    ]
    assert await missing(client, owner, org, user_id=dan.id, manager_user_id=lead.id) == []
    # Without time.read_all: the people the caller manages, and nobody else.
    assert names(await missing(client, lead, org)) == ["Ann Adams", "Bob Brown"]
    assert names(await missing(client, lead, org, user_id=ann.id)) == ["Ann Adams"]
    assert names(await missing(client, lead, org, manager_user_id=lead.id)) == [
        "Ann Adams",
        "Bob Brown",
    ]
    await missing(client, lead, org, status=403, user_id=dan.id)
    await missing(client, lead, org, status=403, manager_user_id=owner.id)
    assert await missing(client, stranger, org) == []  # manages nobody
    await missing(client, stranger, org, status=403, user_id=ann.id)
    # Their own status is theirs to see.
    await set_member(org, stranger, weekly_capacity_minutes=2250)
    assert names(await missing(client, stranger, org, user_id="me")) == ["Sam Stranger"]
    # Unknown people and other practices.
    for params in ({"user_id": uuid.uuid4()}, {"manager_user_id": uuid.uuid4()},
                   {"user_id": outsider.id}, {"manager_user_id": outsider.id}):  # fmt: skip
        error = await missing(client, owner, org, status=404, **params)
        assert error["code"] == "RESOURCE_NOT_FOUND"
    await missing(client, owner, org, status=422, user_id="not-an-id")
    await missing(client, owner, org, status=422, week_start="soon")
    await missing(client, outsider, org, status=404)


# --- Reminders ------------------------------------------------------------------------------


async def reminder_setup(client: AsyncClient, monkeypatch):
    """Ann has nothing, Bob has an open week, Cat and Dan have submitted (Cat's approver is
    Mia, Dan has none so every approver is asked), Mia and the owner do not record time."""
    pin_today(monkeypatch, OVER)
    owner, org = await firm(client)
    mia = await staff(org, "manager", full_name="Mia Manager")
    await set_member(org, mia, weekly_capacity_minutes=0)
    ann = await staff(org, full_name="Ann Adams")
    bob = await staff(org, full_name="Bob Brown")
    cat = await staff(org, full_name="Cat Clark")
    dan = await staff(org, full_name="Dan Dale")
    await set_manager(org, cat, mia)
    await log_time(client, bob, org)
    await submitted(client, org, cat)
    await submitted(client, org, dan)
    return owner, org, mia, ann, bob, cat, dan


async def reminders_of(client: AsyncClient, actor, org: dict, type_: str) -> list[dict]:
    """The reminders in a person's inbox. (The notice sent when a sheet is submitted is of the
    same type as the approvers' reminder; its title is in the singular.)"""
    notes = [n for n in await inbox(client, actor, org) if n["type"] == type_]
    if type_ == "approval_pending":
        notes = [n for n in notes if n["title"] == "Timesheets waiting for approval"]
    return notes


async def test_reminders_are_sent_once_per_person_and_week(
    client: AsyncClient, monkeypatch
) -> None:
    owner, org, mia, ann, bob, cat, dan = await reminder_setup(client, monkeypatch)
    result = await run_reminders(client, owner, org)
    assert result == {
        "week_start": day(0),
        "missing": 2,
        "reminded": 2,
        "already_reminded": 0,
        "unreachable": 0,
        "sheets_waiting": 2,
        "approvers_reminded": 2,
        "approvers_already_reminded": 0,
        "disabled": [],
    }
    # The people who are missing hear of it; the people who submitted do not.
    for person in (ann, bob):
        [note] = await reminders_of(client, person, org, "timesheet_reminder")
        assert note["title"] == "Timesheet reminder" and note["link"] == "/time"
        assert "07/09/2026" in note["body"]
    for person in (cat, dan):
        assert await reminders_of(client, person, org, "timesheet_reminder") == []
    # Approvers hear of the sheets waiting for them: Mia of both, the owner of Dan's.
    [for_mia] = await reminders_of(client, mia, org, "approval_pending")
    assert for_mia["title"] == "Timesheets waiting for approval"
    assert (
        for_mia["link"] == "/time/approvals"
        and "2 timesheets from 2 staff members" in for_mia["body"]
    )
    [for_owner] = await reminders_of(client, owner, org, "approval_pending")
    assert "1 timesheet from Dan Dale is waiting" in for_owner["body"]
    assert "07/09/2026" in for_owner["body"]
    assert [r["user_id"] for r in await reminder_rows(org, "timesheet")] != []
    assert (
        len(await reminder_rows(org, "timesheet")) == 2
        and len(await reminder_rows(org, "approval")) == 2
    )
    rows = {row["name"]: row for row in await missing(client, owner, org)}
    assert rows["Ann Adams"]["last_reminded_at"] and rows["Bob Brown"]["last_reminded_at"]

    # Again: nobody is reminded twice about the same week.
    again = await run_reminders(client, owner, org)
    assert (again["reminded"], again["already_reminded"]) == (0, 2)
    assert (again["approvers_reminded"], again["approvers_already_reminded"]) == (0, 2)
    assert len(await reminders_of(client, ann, org, "timesheet_reminder")) == 1
    assert len(await reminders_of(client, mia, org, "approval_pending")) == 1

    # Someone who submits drops out; a new week is a new reminder.
    await sheet_action(
        client, ann, org, (await log_time(client, ann, org))["timesheet_id"], "submit"
    )
    third = await run_reminders(client, owner, org)
    assert (third["missing"], third["reminded"], third["already_reminded"]) == (1, 0, 1)
    next_week = await run_reminders(client, owner, org, week_start=day(9))
    assert next_week["week_start"] == day(7) and next_week["missing"] == 4
    assert next_week["reminded"] == 4 and next_week["approvers_reminded"] == 2
    assert len(await reminders_of(client, ann, org, "timesheet_reminder")) == 2
    assert len(await reminders_of(client, mia, org, "approval_pending")) == 2


async def test_people_who_cannot_be_reached_are_counted_not_reminded(
    client: AsyncClient, monkeypatch
) -> None:
    owner, org, _, ann, _, _, _ = await reminder_setup(client, monkeypatch)
    placeholder = await staff(org, full_name="Pam Placeholder")
    await set_member(org, placeholder, login_enabled=False)
    listed = {row["name"]: row for row in await missing(client, owner, org)}
    assert listed["Pam Placeholder"]["has_login"] is False and listed["Ann Adams"]["has_login"]
    result = await run_reminders(client, owner, org)
    assert (result["missing"], result["reminded"], result["unreachable"]) == (3, 2, 1)
    assert placeholder.id not in {r["user_id"] for r in await reminder_rows(org, "timesheet")}
    assert ann.id in {r["user_id"] for r in await reminder_rows(org, "timesheet")}


async def test_a_switched_off_reminder_is_not_sent_or_recorded(
    client: AsyncClient, monkeypatch
) -> None:
    owner, org, _, ann, _, _, _ = await reminder_setup(client, monkeypatch)
    off = [
        {"type": t, "enabled": False, "in_app": True, "email": True, "sms": False}
        for t in ("timesheet_reminder", "approval_pending")
    ]
    await put_practice(client, owner, org, {"types": off})
    result = await run_reminders(client, owner, org)
    assert result["disabled"] == ["timesheet_reminder", "approval_pending"]
    assert (result["reminded"], result["approvers_reminded"], result["sheets_waiting"]) == (0, 0, 2)
    assert await reminder_rows(org) == []
    assert await reminders_of(client, ann, org, "timesheet_reminder") == []
    # Switched on again, the next run sends them.
    on = [{**row, "enabled": True} for row in off]
    await put_practice(client, owner, org, {"types": on})
    result = await run_reminders(client, owner, org)
    assert (result["reminded"], result["approvers_reminded"], result["disabled"]) == (2, 2, [])


async def test_two_runs_at_once_remind_everyone_once(client: AsyncClient, monkeypatch) -> None:
    pin_today(monkeypatch, OVER)
    owner, org = await firm(client)
    people = [await staff(org, full_name=f"Person {i:02d}") for i in range(6)]
    first, second = await asyncio.gather(
        run_reminders(client, owner, org), run_reminders(client, owner, org)
    )
    assert first["reminded"] + second["reminded"] == 6
    assert first["already_reminded"] + second["already_reminded"] == 6
    assert len(await reminder_rows(org, "timesheet")) == 6
    for person in people:
        assert len(await reminders_of(client, person, org, "timesheet_reminder")) == 1


async def test_who_may_run_the_reminders(client: AsyncClient, monkeypatch) -> None:
    owner, org, mia, ann, _, _, _ = await reminder_setup(client, monkeypatch)
    _, other = await practice(client, "Other & Co")
    outsider = await staff(other)
    viewer = await staff(org, "viewer")
    billing = await staff(org, "billing")
    for member in (viewer, billing):
        await set_member(org, member, weekly_capacity_minutes=0)
    for actor in (ann, viewer, billing):
        error = await run_reminders(client, actor, org, status=403)
        assert error["code"] == "PERMISSION_DENIED"
    await run_reminders(client, outsider, org, status=404)
    assert await reminder_rows(org) == []

    # With only timesheet.approve the run covers the people the caller manages.
    response = await client.post(
        org_url(org, "/roles"),
        headers=owner.headers,
        json={"name": "Approver", "permissions": ["timesheet.approve"]},
    )
    assert response.status_code == 201, response.text
    approver = await staff(org, "Approver", full_name="Ada Approver")
    await set_member(org, approver, weekly_capacity_minutes=0)
    await set_manager(org, ann, approver)
    scoped = await run_reminders(client, approver, org)
    assert (scoped["missing"], scoped["reminded"], scoped["sheets_waiting"]) == (1, 1, 0)
    assert {r["user_id"] for r in await reminder_rows(org, "timesheet")} == {ann.id}
    # A manager (who reads all time) and the owner cover the whole practice.
    wide = await run_reminders(client, mia, org)
    assert (wide["missing"], wide["reminded"], wide["already_reminded"]) == (2, 1, 1)
    assert wide["sheets_waiting"] == 2
    # The body is optional.
    bare = await client.post(org_url(org, "/timesheets/reminders/run"), headers=owner.headers)
    assert bare.status_code == 200, bare.text
    for body in ({"week_start": "soon"}, {"extra": 1}):
        error = await run_reminders(client, owner, org, status=422, **body)
        assert error["code"] == "VALIDATION_ERROR"


async def test_a_reminder_run_is_audited(client: AsyncClient, monkeypatch) -> None:
    owner, org, *_ = await reminder_setup(client, monkeypatch)
    await run_reminders(client, owner, org)
    [entry] = await audit_entries(org["id"], "timesheet.reminders_sent")
    assert entry["actor_user_id"] == str(owner.id)
    assert entry["metadata"]["reminded"] == 2 and entry["metadata"]["source"] == "manual"
    assert entry["metadata"]["week_start"] == day(0)


# --- The daily job --------------------------------------------------------------------------


async def test_the_worker_sends_the_reminders_of_every_practice_daily(
    client: AsyncClient, worker, monkeypatch
) -> None:
    pin_today(monkeypatch, OVER)
    owner, org = await firm(client)
    ann = await staff(org, full_name="Ann Adams")
    _, other = await firm(client)
    ben = await staff(other, full_name="Ben Brown")

    await worker.tick()
    await worker.tick()  # the same day: still one job
    jobs = await sql("SELECT status FROM background_jobs WHERE type = 'send_timesheet_reminders'")
    assert len(jobs) == 1
    await run_jobs(worker)
    done = await sql(
        "SELECT status, payload FROM background_jobs WHERE type = 'send_timesheet_reminders'"
    )
    assert done[0]["status"] == "succeeded"
    assert done[0]["payload"] == {
        "practices": 2,
        "failed": 0,
        "reminded": 2,
        "approvers_reminded": 0,
    }
    assert len(await reminders_of(client, ann, org, "timesheet_reminder")) == 1
    assert len(await reminders_of(client, ben, other, "timesheet_reminder")) == 1
    assert await reminders_of(client, owner, org, "timesheet_reminder") == []
    assert [
        e["metadata"]["source"] for e in await audit_entries(org["id"], "timesheet.reminders_sent")
    ] == ["scheduler"]

    # The next day's job sends nothing again.
    await sql("DELETE FROM background_jobs WHERE type = 'send_timesheet_reminders'")
    async with get_sessionmaker()() as session:
        await job_queue.enqueue(session, "send_timesheet_reminders", dedupe_key="next-day")
        await session.commit()
    await run_jobs(worker)
    assert len(await reminders_of(client, ann, org, "timesheet_reminder")) == 1
    assert len(await audit_entries(org["id"], "timesheet.reminders_sent")) == 1  # nothing sent


async def test_a_practice_that_fails_does_not_stop_the_others(
    client: AsyncClient, monkeypatch
) -> None:
    pin_today(monkeypatch, OVER)
    _, org = await firm(client)
    _, other = await firm(client)
    ann = await staff(org, full_name="Ann Adams")
    ben = await staff(other, full_name="Ben Brown")
    real = reminders_service.run_reminders

    async def flaky(session, organization_id, **kwargs):
        if str(organization_id) == org["id"]:
            raise RuntimeError("boom")
        return await real(session, organization_id, **kwargs)

    monkeypatch.setattr(reminders_service, "run_reminders", flaky)
    async with get_sessionmaker()() as session:
        totals = await reminders_service.run_for_all_practices(session)
    assert totals == {"practices": 1, "failed": 1, "reminded": 1, "approvers_reminded": 0}
    assert len(await reminder_rows(other)) == 1 and await reminder_rows(org) == []
    assert len(await reminders_of(client, ben, other, "timesheet_reminder")) == 1
    assert await reminders_of(client, ann, org, "timesheet_reminder") == []
