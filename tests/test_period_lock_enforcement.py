"""A locked period is closed for time, expenses and WIP: every write path that creates, edits or
deletes dated data of those kinds refuses it (409 PERIOD_LOCKED), on the boundary day and before it,
for everybody, while reads, invoices and payments carry on."""

import uuid
from datetime import date, timedelta

import pytest

from app.db.session import get_sessionmaker
from app.services import period_locks
from tests.billing_helpers import billing_status, count_rows, draft, issued, new_practice, pay
from tests.clients_helpers import count_queries, sql
from tests.domain_factories import (
    make_client,
    make_disbursement,
    make_time_entry,
    practice,
    staff,
)
from tests.factories import audit_entries
from tests.period_lock_helpers import assert_locked, lock_period, org_url, set_lock, today
from tests.time_helpers import entry_row, make_client_rate, ok_data

DAY = timedelta(days=1)


def entry_body(p, day: date, minutes: int = 60, **extra) -> dict:
    return {"entry_date": str(day), "minutes": minutes, "client_id": str(p.client.id), **extra}


def expense_body(p, day: date, **extra) -> dict:
    return {
        "client_id": str(p.client.id),
        "incurred_on": str(day),
        "description": "Filing fee",
        "amount": 1_300,
        **extra,
    }


async def scene(client):
    """A practice with a staff member and a lock through ten days ago."""
    p = await new_practice(client)
    worker = await staff(p.org, "staff")
    lock = today(p.org) - 10 * DAY
    await set_lock(p.org, lock)
    return p, worker, lock


# --- Time --------------------------------------------------------------------------------------


async def test_time_cannot_be_logged_in_a_locked_period(client):
    p, worker, lock = await scene(client)
    # Nobody is exempt, the Owner included; the boundary day itself is locked.
    for actor in (worker, p.owner):
        for day in (lock, lock - DAY, lock - 200 * DAY):
            response = await client.post(
                p.url("/time-entries"), headers=actor.headers, json=entry_body(p, day)
            )
            assert_locked(response, day, lock, "time")
    assert await count_rows("SELECT count(*) FROM time_entries") == 0
    # The next day is open.
    created = await client.post(
        p.url("/time-entries"), headers=worker.headers, json=entry_body(p, lock + DAY)
    )
    assert created.status_code == 201, created.text
    # The lock is about the date of the time, not the day it is recorded: later days still work.
    assert await count_rows("SELECT count(*) FROM time_entries") == 1


async def test_a_bulk_batch_with_one_locked_day_writes_nothing(client):
    p, worker, lock = await scene(client)
    batch = [
        entry_body(p, lock + DAY),
        entry_body(p, lock + 2 * DAY),
        entry_body(p, lock - 3 * DAY),
    ]
    refused = await client.post(
        p.url("/time-entries/bulk"), headers=worker.headers, json={"entries": batch}
    )
    # The earliest locked day is the one named.
    assert_locked(refused, lock - 3 * DAY, lock, "time")
    assert await count_rows("SELECT count(*) FROM time_entries") == 0
    accepted = await client.post(
        p.url("/time-entries/bulk"), headers=worker.headers, json={"entries": batch[:2]}
    )
    assert accepted.status_code == 201, accepted.text
    assert await count_rows("SELECT count(*) FROM time_entries") == 2


async def test_locked_time_cannot_be_edited_or_moved_and_open_time_cannot_move_in(client):
    p, worker, lock = await scene(client)
    inside = await make_time_entry(
        p.org, worker, client=p.client, entry_date=lock - 2 * DAY, timesheet_status="open"
    )
    outside = await make_time_entry(
        p.org, worker, client=p.client, entry_date=lock + 3 * DAY, timesheet_status="open"
    )
    in_url = p.url(f"/time-entries/{inside.id}")
    out_url = p.url(f"/time-entries/{outside.id}")

    # Any change to locked time is refused, even a note ...
    typo = await client.patch(in_url, headers=worker.headers, json={"description": "Typo"})
    assert_locked(typo, lock - 2 * DAY, lock, "time")
    # ... and so is moving it out of the period.
    away = await client.patch(
        in_url, headers=worker.headers, json={"entry_date": str(lock + 4 * DAY)}
    )
    assert_locked(away, lock - 2 * DAY, lock, "time")
    # Open time cannot be moved into the period, on its last day or before it.
    boundary = await client.patch(out_url, headers=worker.headers, json={"entry_date": str(lock)})
    assert_locked(boundary, lock, lock, "time")
    earlier = await client.patch(
        out_url, headers=worker.headers, json={"entry_date": str(lock - 5 * DAY)}
    )
    assert_locked(earlier, lock - 5 * DAY, lock, "time")
    assert (await entry_row(inside.id)).entry_date == lock - 2 * DAY
    assert (await entry_row(outside.id)).entry_date == lock + 3 * DAY

    # Open time stays editable, and may move to another open day.
    edited = await client.patch(
        out_url, headers=worker.headers, json={"description": "Fine", "minutes": 30}
    )
    assert edited.status_code == 200, edited.text
    moved = await client.patch(
        out_url, headers=worker.headers, json={"entry_date": str(lock + DAY)}
    )
    assert moved.status_code == 200, moved.text
    assert (await entry_row(outside.id)).entry_date == lock + DAY


async def test_locked_time_cannot_be_deleted(client):
    p, worker, lock = await scene(client)
    inside = await make_time_entry(
        p.org, worker, client=p.client, entry_date=lock - 2 * DAY, timesheet_status="open"
    )
    boundary = await make_time_entry(
        p.org, worker, client=p.client, entry_date=lock, timesheet_status="open"
    )
    outside = await make_time_entry(
        p.org, worker, client=p.client, entry_date=lock + DAY, timesheet_status="open"
    )
    assert_locked(
        await client.delete(p.url(f"/time-entries/{inside.id}"), headers=worker.headers),
        lock - 2 * DAY,
        lock,
        "time",
    )
    assert_locked(
        await client.delete(p.url(f"/time-entries/{boundary.id}"), headers=p.owner.headers),
        lock,
        lock,
        "time",
    )
    assert await count_rows("SELECT count(*) FROM time_entries") == 3
    gone = await client.delete(p.url(f"/time-entries/{outside.id}"), headers=worker.headers)
    assert gone.status_code == 200, gone.text
    assert await count_rows("SELECT count(*) FROM time_entries") == 2


async def test_a_timer_started_in_a_period_that_gets_locked_cannot_be_stopped(client):
    p = await new_practice(client)
    worker = await staff(p.org)
    started = ok_data(
        await client.post(
            p.url("/timer/start"), headers=worker.headers, json={"client_id": str(p.client.id)}
        ),
        201,
    )
    day = date.fromisoformat(started["started_on"])
    await set_lock(p.org, day)

    assert_locked(await client.post(p.url("/timer/stop"), headers=worker.headers), day, day, "time")
    # The timer is kept, and so is the time it would have written.
    assert ok_data(await client.get(p.url("/timer"), headers=worker.headers))["id"] == started["id"]
    assert await count_rows("SELECT count(*) FROM time_entries") == 0
    # Replacing it stops it first, which is refused as well.
    replaced = await client.post(
        p.url("/timer/start"), headers=worker.headers, json={"replace": True}
    )
    assert_locked(replaced, day, day, "time")
    assert ok_data(await client.get(p.url("/timer"), headers=worker.headers))["id"] == started["id"]
    # Discarding needs no time to be written.
    assert (await client.delete(p.url("/timer"), headers=worker.headers)).status_code == 200

    await set_lock(p.org, None)
    ok_data(await client.post(p.url("/timer/start"), headers=worker.headers, json={}), 201)
    ok_data(await client.post(p.url("/timer/stop"), headers=worker.headers), 201)


async def test_timesheet_decisions_still_work_in_a_locked_week(client):
    p = await new_practice(client)
    worker = await staff(p.org)
    manager = await staff(p.org, "manager")
    t = today(p.org)
    day, lock = t - 20 * DAY, t - 5 * DAY
    entry = await make_time_entry(
        p.org, worker, client=p.client, entry_date=day, timesheet_status="open"
    )
    await set_lock(p.org, lock)
    sheet = p.url(f"/timesheets/{entry.timesheet_id}")

    # Submitting, recalling, returning, approving and reopening change the sheet, not the time.
    steps = (
        (worker, "submit", None),
        (worker, "recall", None),
        (worker, "submit", None),
        (manager, "reject", {"decision_note": "Check Tuesday"}),
        (worker, "submit", None),
        (manager, "approve", None),
        (manager, "reopen", None),
    )
    for actor, action, body in steps:
        response = await client.post(f"{sheet}/{action}", headers=actor.headers, json=body)
        assert response.status_code == 200, (action, response.text)
    # The time itself stays closed.
    refused = await client.patch(
        p.url(f"/time-entries/{entry.id}"), headers=worker.headers, json={"minutes": 30}
    )
    assert_locked(refused, day, lock, "time")


# --- Expenses (disbursements) ------------------------------------------------------------------


async def test_a_disbursement_cannot_be_recorded_in_a_locked_period(client):
    p, worker, lock = await scene(client)
    for actor in (worker, p.owner):
        for day in (lock, lock - DAY, lock - 200 * DAY):
            response = await client.post(
                p.url("/disbursements"), headers=actor.headers, json=expense_body(p, day)
            )
            assert_locked(response, day, lock, "expense")
    assert await count_rows("SELECT count(*) FROM disbursements") == 0
    created = await client.post(
        p.url("/disbursements"), headers=worker.headers, json=expense_body(p, lock + DAY)
    )
    assert created.status_code == 201, created.text


async def test_a_locked_disbursement_cannot_be_edited_moved_or_deleted(client):
    p, _, lock = await scene(client)
    inside = await make_disbursement(p.org, p.client, incurred_on=lock - 3 * DAY)
    outside = await make_disbursement(p.org, p.client, incurred_on=lock + 2 * DAY)
    in_url = p.url(f"/disbursements/{inside.id}")
    out_url = p.url(f"/disbursements/{outside.id}")

    assert_locked(
        await client.patch(in_url, headers=p.owner.headers, json={"description": "Typo"}),
        lock - 3 * DAY,
        lock,
        "expense",
    )
    assert_locked(
        await client.patch(
            in_url, headers=p.owner.headers, json={"incurred_on": str(lock + 5 * DAY)}
        ),
        lock - 3 * DAY,
        lock,
        "expense",
    )
    assert_locked(
        await client.patch(out_url, headers=p.owner.headers, json={"incurred_on": str(lock)}),
        lock,
        lock,
        "expense",
    )
    assert_locked(
        await client.delete(in_url, headers=p.owner.headers), lock - 3 * DAY, lock, "expense"
    )
    assert await count_rows("SELECT count(*) FROM disbursements") == 2

    edited = await client.patch(
        out_url, headers=p.owner.headers, json={"incurred_on": str(lock + DAY), "amount": 2_000}
    )
    assert edited.status_code == 200, edited.text
    assert (await client.delete(out_url, headers=p.owner.headers)).status_code == 200
    assert await count_rows("SELECT count(*) FROM disbursements") == 1


# --- WIP ---------------------------------------------------------------------------------------


async def test_a_write_off_dated_in_a_locked_period_is_refused(client):
    p, worker, lock = await scene(client)
    entry = await make_time_entry(p.org, worker, client=p.client, entry_date=lock - 30 * DAY)
    body = {
        "client_id": str(p.client.id),
        "items": [{"time_entry_id": str(entry.id)}],
        "reason": "Agreed with the client",
    }
    url = p.url("/wip/write-offs")
    for day in (lock, lock - 40 * DAY):
        refused = await client.post(
            url, headers=p.owner.headers, json={**body, "written_off_on": str(day)}
        )
        assert_locked(refused, day, lock, "wip")
    assert await count_rows("SELECT count(*) FROM wip_write_offs") == 0
    assert (await billing_status("time_entries", [entry.id]))[entry.id] == "unbilled"

    # Without a date it is dated today, which a lock through today also covers.
    t = today(p.org)
    await set_lock(p.org, t)
    assert_locked(await client.post(url, headers=p.owner.headers, json=body), t, t, "wip")
    await set_lock(p.org, lock)

    written = await client.post(
        url, headers=p.owner.headers, json={**body, "written_off_on": str(lock + DAY)}
    )
    assert written.status_code == 201, written.text
    assert (await billing_status("time_entries", [entry.id]))[entry.id] == "written_off"


async def test_a_write_off_in_a_locked_period_cannot_be_reversed(client):
    p, worker, _ = await scene(client)
    t = today(p.org)
    await set_lock(p.org, None)
    early = await make_time_entry(p.org, worker, client=p.client, entry_date=t - 40 * DAY)
    late = await make_time_entry(p.org, worker, client=p.client, entry_date=t - 30 * DAY)
    ids = {}
    for name, entry, day in (("early", early, t - 20 * DAY), ("late", late, t - 5 * DAY)):
        response = await client.post(
            p.url("/wip/write-offs"),
            headers=p.owner.headers,
            json={
                "client_id": str(p.client.id),
                "items": [{"time_entry_id": str(entry.id)}],
                "reason": "Not recoverable",
                "written_off_on": str(day),
            },
        )
        assert response.status_code == 201, response.text
        ids[name] = response.json()["data"]["id"]

    lock = t - 10 * DAY
    await set_lock(p.org, lock)
    refused = await client.delete(p.url(f"/wip/write-offs/{ids['early']}"), headers=p.owner.headers)
    assert_locked(refused, t - 20 * DAY, lock, "wip")
    assert (await billing_status("time_entries", [early.id]))[early.id] == "written_off"
    assert await count_rows("SELECT count(*) FROM wip_write_offs") == 2

    reversed_ = await client.delete(
        p.url(f"/wip/write-offs/{ids['late']}"), headers=p.owner.headers
    )
    assert reversed_.status_code == 200, reversed_.text
    assert (await billing_status("time_entries", [late.id]))[late.id] == "unbilled"
    # Reading what is in the locked period is fine.
    assert (
        await client.get(p.url(f"/wip/write-offs/{ids['early']}"), headers=p.owner.headers)
    ).status_code == 200


# --- What is not blocked -----------------------------------------------------------------------


async def test_reads_invoices_and_payments_are_not_blocked(client):
    p = await new_practice(client)
    worker = await staff(p.org)
    t = today(p.org)
    old = t - 20 * DAY
    entry = await make_time_entry(p.org, worker, client=p.client, entry_date=old)
    expense = await make_disbursement(p.org, p.client, incurred_on=old)
    await set_lock(p.org, t)

    for path in (
        "/time-entries",
        f"/time-entries/{entry.id}",
        "/timesheets",
        f"/timesheets/{entry.timesheet_id}",
        "/disbursements",
        f"/disbursements/{expense.id}",
        "/wip",
        f"/clients/{p.client.id}/wip",
        "/wip/write-offs",
        "/period-locks",
        "/period-locks/history",
    ):
        response = await client.get(p.url(path), headers=p.owner.headers)
        assert response.status_code == 200, (path, response.text)
    week = await client.get(
        p.url("/time-entries/week"), headers=worker.headers, params={"week_start": str(old)}
    )
    assert week.status_code == 200, week.text

    # Billing the locked WIP, taking payment and the rest of the money side carry on.
    invoice = await draft(
        client, p.owner, p.org, p.client.id, time=[entry], disbursements=[expense]
    )
    await issued(client, p.owner, p.org, invoice["id"])
    received = await pay(client, p.owner, p.org, p.client.id, 5_000, received_on=old)
    assert received.status_code == 201, received.text


async def test_rerating_skips_time_in_a_locked_period_with_the_reason(client):
    owner, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    t = today(org)
    lock = t - 10 * DAY
    inside = await make_time_entry(
        org, person, client=acme, entry_date=lock - DAY, charge_rate=6_000, cost_rate=1_000
    )
    outside = await make_time_entry(
        org, person, client=acme, entry_date=lock + DAY, charge_rate=6_000, cost_rate=1_000
    )
    await make_client_rate(org, acme.id, 12_000)
    await set_lock(org, lock)
    scope = {"date_from": str(lock - 30 * DAY), "date_to": str(t)}

    preview = ok_data(
        await client.post(
            org_url(org, "/rates/rerate/preview"), headers=owner.headers, json={"scope": scope}
        )
    )
    assert preview["skipped"]["period_locked"] == 1
    assert preview["totals"]["entries_to_change"] == 1
    [skipped] = [i for i in preview["items"] if i["outcome"] == "skipped"]
    assert skipped["entry_id"] == str(inside.id)
    assert skipped["skip_reason"] == f"Period locked until {lock:%d/%m/%Y}"

    applied = await client.post(
        org_url(org, "/rates/rerate"),
        headers=owner.headers,
        json={"scope": scope, "preview_token": preview["preview_token"]},
    )
    run = ok_data(applied, 201)
    assert (run["entries_changed"], run["entries_skipped"]) == (1, 1)
    assert (await entry_row(inside.id)).charge_rate == 6_000
    assert (await entry_row(outside.id)).charge_rate == 12_000

    # The direct V1 re-pricing leaves it alone as well.
    await make_client_rate(org, acme.id, 15_000, person)
    revalued = ok_data(
        await client.post(org_url(org, "/rates/revalue"), headers=owner.headers, json=scope)
    )
    assert revalued["entries_changed"] == 1
    assert (await entry_row(inside.id)).charge_rate == 6_000
    assert (await entry_row(outside.id)).charge_rate == 15_000


# --- The service -------------------------------------------------------------------------------


async def test_assert_open_assert_open_many_and_locked_dates(client):
    _, org = await practice(client)
    _, other = await practice(client, "Other & Co")
    org_id, other_id = uuid.UUID(org["id"]), uuid.UUID(other["id"])
    lock = today(org) - 10 * DAY

    async with get_sessionmaker()() as session:
        # Nothing locked: every day is open, and no day is reported.
        await period_locks.assert_open(session, org_id, date(2000, 1, 1), "time")
        assert await period_locks.locked_dates(session, org_id, [lock, lock + DAY]) == {}
    await set_lock(org, lock)

    reason = f"Period locked until {lock:%d/%m/%Y}"
    async with get_sessionmaker()() as session:
        for area in ("time", "expense", "wip"):
            with pytest.raises(period_locks.PeriodLockedError) as caught:
                await period_locks.assert_open(session, org_id, lock, area)
            error = caught.value
            assert (error.status_code, error.code) == (409, "PERIOD_LOCKED")
            assert error.details == {"date": str(lock), "locked_through": str(lock), "area": area}
            assert f"{lock:%d/%m/%Y}" in error.message
        await period_locks.assert_open(session, org_id, lock + DAY, "time")

        with pytest.raises(period_locks.PeriodLockedError) as caught:
            await period_locks.assert_open_many(
                session, org_id, {lock + DAY, lock - 3 * DAY, lock - DAY}, "expense"
            )
        assert caught.value.details["date"] == str(lock - 3 * DAY)
        assert caught.value.details["area"] == "expense"
        await period_locks.assert_open_many(session, org_id, [lock + DAY, lock + 2 * DAY], "wip")
        await period_locks.assert_open_many(session, org_id, [], "time")

        assert await period_locks.locked_dates(session, org_id, {lock - DAY, lock, lock + DAY}) == {
            lock - DAY: reason,
            lock: reason,
        }
        assert await period_locks.locked_dates(session, org_id, []) == {}

        # One lookup (the lock, then the periods) however many days; none at all for no days.
        with count_queries() as statements:
            await period_locks.assert_open_many(
                session, org_id, [lock + n * DAY for n in (1, 2, 3)], "time"
            )
        assert len(statements) == 2
        with count_queries() as statements:
            await period_locks.assert_open_many(session, org_id, [], "time")
        assert statements == []

        # Another practice is not affected.
        await period_locks.assert_open(session, other_id, lock - 100 * DAY, "time")
        assert await period_locks.locked_dates(session, other_id, [lock]) == {}


async def test_a_practice_without_a_lock_row_is_open_and_can_still_be_locked(client):
    owner, org = await practice(client)
    org_id = uuid.UUID(org["id"])
    t = today(org)

    await sql("DELETE FROM period_locks WHERE organization_id = :o", o=org["id"])
    async with get_sessionmaker()() as session:
        await period_locks.assert_open(session, org_id, t - 400 * DAY, "time")
    assert (await lock_period(client, owner, org, "year", t.year - 1)).status_code == 200
    assert (
        await count_rows(
            "SELECT count(*) FROM period_locks WHERE organization_id = :o", o=org["id"]
        )
        == 1
    )
    entries = await audit_entries(org["id"], "period_lock.locked")
    assert len(entries) == 1
