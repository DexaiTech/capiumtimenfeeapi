"""Copying a week and copying one entry: validated like new entries, partial success."""

import json
from datetime import date

import pytest
from httpx import AsyncClient

from app.utils.money import time_value
from tests.clients_helpers import sql
from tests.domain_factories import (
    activity_code_id,
    make_client,
    make_job,
    make_staff_rate,
    practice,
    staff,
)
from tests.factories import audit_entries
from tests.period_lock_helpers import set_lock
from tests.time_gap_helpers import (
    copy_entry,
    copy_week,
    day,
    get_sheet,
    set_member,
    sheet_action,
)
from tests.time_helpers import (
    log_time,
    ok_data,
    org_url,
    set_practice,
    sheet_count,
)


async def entries_between(client: AsyncClient, actor, org: dict, first: str, last: str, **params):
    response = await client.get(
        org_url(org, "/time-entries"),
        headers=actor.headers,
        params={"date_from": first, "date_to": last, "limit": 100, **params},
    )
    return sorted(ok_data(response), key=lambda e: (e["entry_date"], e["created_at"]))


# --- Copying a week -------------------------------------------------------------------------


async def test_copy_week_copies_every_entry_into_the_next_week(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    job = await make_job(org, acme, "Accounts")
    acc = await activity_code_id(org, "ACC")
    await make_staff_rate(org, person, 10_000, 4_000)
    await make_staff_rate(org, person, 12_000, 5_000, effective_from=date(2026, 9, 14))
    await log_time(
        client, person, org, entry_date=day(0), job_id=job.id, activity_code_id=acc,
        minutes=120, description="Accounts", internal_note="Check the loan",
    )  # fmt: skip
    await log_time(client, person, org, entry_date=day(1), minutes=60, description="Training")
    await log_time(client, person, org, entry_date=day(2), client_id=acme.id, minutes=90)

    # Any day of either week will do.
    result = await copy_week(client, person, org, from_week_start=day(2), to_week_start=day(10))
    assert (result["from_week_start"], result["to_week_start"]) == (day(0), day(7))
    assert result["skipped"] == []
    created = result["created"]
    assert [e["entry_date"] for e in created] == [day(7), day(8), day(9)]
    assert [e["minutes"] for e in created] == [120, 60, 90]
    assert [e["is_billable"] for e in created] == [True, False, True]
    assert created[0]["job_id"] == str(job.id) and created[0]["client_id"] == str(acme.id)
    assert created[0]["activity_code_id"] == str(acc)
    assert [e["description"] for e in created] == ["Accounts", "Training", None]
    assert [e["internal_note"] for e in created] == [None, None, None]  # notes stay behind
    assert all(e["created_by"] == str(person.id) for e in created)
    assert all(e["user_id"] == str(person.id) for e in created)
    # The copies are new entries: the rate of the new date applies.
    assert created[0]["charge_rate"] == 12_000
    assert created[0]["charge_value"] == time_value(120, 12_000) == 24_000
    assert len({e["timesheet_id"] for e in created}) == 1
    assert created[0]["timesheet_status"] == "open"
    # The week they came from is untouched.
    original = await entries_between(client, person, org, day(0), day(6))
    assert [e["charge_rate"] for e in original] == [10_000, 10_000, 10_000]
    assert original[0]["internal_note"] == "Check the loan"

    # The notes come along when asked for.
    again = await copy_week(
        client, person, org, from_week_start=day(0), to_week_start=day(14), include_notes=True
    )
    assert [e["internal_note"] for e in again["created"]] == ["Check the loan", None, None]


async def test_copy_week_reports_what_it_skips_and_creates_the_rest(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    open_job = await make_job(org, acme, "Open job")
    closing_job = await make_job(org, acme, "Closing job")
    gone = await make_client(org, "Gone Ltd")
    first = await log_time(client, person, org, entry_date=day(0), job_id=open_job.id)
    closed = await log_time(client, person, org, entry_date=day(0), job_id=closing_job.id)
    heavy = await log_time(client, person, org, entry_date=day(1), minutes=120)
    inactive = await log_time(client, person, org, entry_date=day(2), client_id=gone.id)
    await sql("UPDATE jobs SET status = 'completed' WHERE id = :i", i=closing_job.id)
    await sql("UPDATE clients SET status = 'inactive' WHERE id = :i", i=gone.id)
    # 23 hours are already booked on the Tuesday of the target week.
    await log_time(client, person, org, entry_date=day(8), minutes=1380)

    result = await copy_week(client, person, org, from_week_start=day(0), to_week_start=day(7))
    assert [e["entry_date"] for e in result["created"]] == [day(7)]
    assert result["created"][0]["job_id"] == str(open_job.id)
    skipped = {s["source_entry_id"]: s for s in result["skipped"]}
    assert set(skipped) == {closed["id"], heavy["id"], inactive["id"]}
    assert skipped[closed["id"]]["code"] == "JOB_CLOSED"
    assert skipped[heavy["id"]]["code"] == "DAY_LIMIT_EXCEEDED"
    assert "1500" in skipped[heavy["id"]]["message"] and "1440" in skipped[heavy["id"]]["message"]
    assert skipped[inactive["id"]]["code"] == "CLIENT_INACTIVE"
    assert all(
        set(s) == {"source_entry_id", "code", "message"} and s["message"] for s in skipped.values()
    )
    assert first["id"] not in skipped
    # Nothing of a skipped copy is left behind.
    target = await entries_between(client, person, org, day(7), day(13))
    assert [(e["entry_date"], e["minutes"]) for e in target] == [(day(7), 60), (day(8), 1380)]


async def test_copies_follow_the_practice_time_increment(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    await set_practice(org, time_increment_minutes=1)
    await log_time(client, person, org, entry_date=day(0), minutes=7)
    await log_time(client, person, org, entry_date=day(1), minutes=16)
    await log_time(client, person, org, entry_date=day(2), minutes=30)
    await set_practice(org, time_increment_minutes=15)
    result = await copy_week(client, person, org, from_week_start=day(0), to_week_start=day(7))
    assert [e["minutes"] for e in result["created"]] == [15, 30, 30]
    single = await copy_entry(client, person, org, result["created"][0]["id"], date=day(3))
    assert single["minutes"] == 15


async def test_a_submitted_or_approved_target_week_refuses_the_copy(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    await log_time(client, person, org, entry_date=day(0))
    target = await log_time(client, person, org, entry_date=day(7), minutes=30)
    sheet_id = target["timesheet_id"]
    await sheet_action(client, person, org, sheet_id, "submit")
    for step in (None, "approve"):
        if step:
            await sheet_action(client, owner, org, sheet_id, step)
        error = await copy_week(
            client, person, org, status=409, from_week_start=day(0), to_week_start=day(7)
        )
        assert error["code"] == "TIMESHEET_NOT_EDITABLE"
        assert error["details"]["timesheet_status"] in ("submitted", "approved")
        assert len(await entries_between(client, person, org, day(7), day(13))) == 1
    # A returned timesheet can be completed.
    await sheet_action(client, owner, org, sheet_id, "reopen")
    await sheet_action(client, person, org, sheet_id, "submit")
    await sheet_action(client, owner, org, sheet_id, "reject", json={"decision_note": "Add more"})
    result = await copy_week(client, person, org, from_week_start=day(0), to_week_start=day(7))
    assert len(result["created"]) == 1
    sheet = await get_sheet(client, person, org, sheet_id)
    assert sheet["status"] == "rejected" and sheet["entry_count"] == 2


async def test_copy_week_request_rules(client: AsyncClient) -> None:
    _, org = await practice(client)
    _, other = await practice(client, "Other & Co")
    person = await staff(org)
    stranger_entry = await log_time(client, await staff(other), other)
    first = await log_time(client, person, org, entry_date=day(0))
    second = await log_time(client, person, org, entry_date=day(1))
    later = await log_time(client, person, org, entry_date=day(8))

    same = await copy_week(
        client, person, org, status=422, from_week_start=day(0), to_week_start=day(4)
    )
    assert same["details"][0]["field"] == "to_week_start"
    for body in ({}, {"from_week_start": day(0)}, {"from_week_start": "x", "to_week_start": day(7)},
                 {"from_week_start": day(0), "to_week_start": day(7), "entry_ids": []},
                 {"from_week_start": day(0), "to_week_start": day(7), "extra": 1}):  # fmt: skip
        await copy_week(client, person, org, status=422, **body)

    # Only some entries.
    chosen = await copy_week(
        client, person, org, from_week_start=day(0), to_week_start=day(14), entry_ids=[second["id"]]
    )
    assert [e["entry_date"] for e in chosen["created"]] == [day(15)]
    # Entries of another week, or of another practice, are unknown.
    for unknown in (later["id"], stranger_entry["id"]):
        error = await copy_week(
            client, person, org, status=404, from_week_start=day(0), to_week_start=day(14),
            entry_ids=[first["id"], unknown],
        )  # fmt: skip
        assert error["code"] == "RESOURCE_NOT_FOUND" and error["details"]["field"] == "entry_ids"
    # Nothing to copy: no timesheet is created for the target week either.
    before = await sheet_count(org)
    empty = await copy_week(client, person, org, from_week_start=day(28), to_week_start=day(35))
    assert empty["created"] == [] and empty["skipped"] == []
    assert await sheet_count(org) == before


async def test_copy_week_permissions(client: AsyncClient) -> None:
    owner, org = await practice(client)
    _, other = await practice(client, "Other & Co")
    person = await staff(org)
    colleague = await staff(org)
    manager = await staff(org, "manager")
    viewer = await staff(org, "viewer")
    billing = await staff(org, "billing")
    left = await staff(org)
    outsider = await staff(other)
    await log_time(client, person, org, entry_date=day(0), minutes=45)
    body = {"from_week_start": day(0), "to_week_start": day(7)}

    # Own time needs time.log.
    for actor in (viewer, billing):
        await copy_week(client, actor, org, status=403, **body)
    # Someone else's needs time.manage_all.
    await copy_week(client, colleague, org, status=403, user_id=person.id, **body)
    for actor in (viewer, billing):
        await copy_week(client, actor, org, status=403, user_id=person.id, **body)
    result = await copy_week(client, manager, org, user_id=person.id, **body)
    [copy] = result["created"]
    assert copy["user_id"] == str(person.id) and copy["created_by"] == str(manager.id)
    # The person must be an active member of this practice.
    await set_member(org, left, status="inactive")
    for user in (left.id, outsider.id):
        error = await copy_week(client, owner, org, status=422, user_id=user, **body)
        assert error["details"][0]["field"] == "user_id"
    # The caller's own week is empty for the manager: nothing to copy, nothing refused.
    assert (await copy_week(client, manager, org, **body))["created"] == []
    # Another practice cannot reach this one.
    await copy_week(client, outsider, org, status=404, **body)


async def test_copies_are_audited(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    closing = await make_job(org, acme)
    kept = await log_time(
        client, person, org, entry_date=day(0), minutes=60, internal_note="Secret"
    )
    dropped = await log_time(client, person, org, entry_date=day(1), job_id=closing.id)
    await sql("UPDATE jobs SET status = 'cancelled' WHERE id = :i", i=closing.id)
    result = await copy_week(
        client, person, org, from_week_start=day(0), to_week_start=day(7), include_notes=True
    )
    [new] = result["created"]
    created = await audit_entries(org["id"], "time_entry.created")
    assert [c["metadata"]["source"] for c in created[-1:]] == ["copy_week"]
    assert created[-1]["target_id"] == new["id"]
    [summary] = await audit_entries(org["id"], "time_entry.week_copied")
    meta = summary["metadata"]
    assert (
        summary["actor_user_id"] == str(person.id) and summary["target_id"] == new["timesheet_id"]
    )
    assert meta["from_week_start"] == day(0) and meta["to_week_start"] == day(7)
    assert meta["created"] == [{"source_entry_id": kept["id"], "entry_id": new["id"]}]
    assert meta["skipped"] == [{"source_entry_id": dropped["id"], "code": "JOB_CLOSED"}]
    assert "Secret" not in json.dumps(await audit_entries(org["id"]))


async def test_copies_are_refused_in_a_locked_period(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    first = await log_time(client, person, org, entry_date=day(0))
    second = await log_time(client, person, org, entry_date=day(1))
    # The target week is locked through its Monday: the copy of the Monday entry is skipped,
    # the Tuesday one is made. (The source week is locked too; reading it is never blocked.)
    await set_lock(org, date.fromisoformat(day(7)))
    result = await copy_week(client, person, org, from_week_start=day(0), to_week_start=day(7))
    assert [e["entry_date"] for e in result["created"]] == [day(8)]
    [skipped] = result["skipped"]
    assert skipped["code"] == "PERIOD_LOCKED" and skipped["source_entry_id"] == first["id"]
    assert "locked" in skipped["message"]
    error = await copy_entry(client, person, org, second["id"], status=409, date=day(7))
    assert error["code"] == "PERIOD_LOCKED"
    assert error["details"] == {"date": day(7), "locked_through": day(7), "area": "time"}
    assert [
        e["entry_date"] for e in await entries_between(client, person, org, day(7), day(13))
    ] == [day(8)]
    # Reopened, the same copy works.
    await set_lock(org, None)
    assert (await copy_entry(client, person, org, second["id"], date=day(7)))["entry_date"] == day(
        7
    )


# --- Copying one entry ----------------------------------------------------------------------


async def test_copy_one_entry_to_another_day(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    job = await make_job(org, acme)
    await make_staff_rate(org, person, 9_000)
    entry = await log_time(
        client, person, org, job_id=job.id, minutes=60, description="Review",
        internal_note="Private", is_billable=False,
    )  # fmt: skip
    copy = await copy_entry(client, person, org, entry["id"], date=day(3))
    assert copy["id"] != entry["id"] and copy["entry_date"] == day(3)
    assert copy["job_id"] == str(job.id) and copy["description"] == "Review"
    assert copy["is_billable"] is False and copy["minutes"] == 60
    assert copy["internal_note"] is None and copy["created_by"] == str(person.id)
    noted = await copy_entry(client, person, org, entry["id"], date=day(4), include_notes=True)
    assert noted["internal_note"] == "Private"
    # The same day, the next week (the alias entry_date is accepted too).
    assert (await copy_entry(client, person, org, entry["id"], date=day(0)))["entry_date"] == day(0)
    later = await copy_entry(client, person, org, entry["id"], entry_date=day(9))
    assert later["entry_date"] == day(9) and later["timesheet_id"] != entry["timesheet_id"]
    audit = await audit_entries(org["id"], "time_entry.copied")
    assert audit[0]["target_id"] == copy["id"]
    assert audit[0]["metadata"]["source_entry_id"] == entry["id"]
    created = await audit_entries(org["id"], "time_entry.created")
    assert created[1]["metadata"]["source"] == "copy"


async def test_a_single_copy_follows_the_rules_of_a_new_entry(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    job = await make_job(org, acme)
    await set_practice(org, time_increment_minutes=1)
    entry = await log_time(client, person, org, job_id=job.id, minutes=7)
    await set_practice(org, time_increment_minutes=6)
    assert (await copy_entry(client, person, org, entry["id"], date=day(1)))["minutes"] == 12
    # The 24-hour day.
    await log_time(client, person, org, entry_date=day(2), minutes=1440)
    error = await copy_entry(client, person, org, entry["id"], status=422, date=day(2))
    assert error["code"] == "DAY_LIMIT_EXCEEDED"
    # A locked target week.
    target = await log_time(client, person, org, entry_date=day(7))
    await sheet_action(client, person, org, target["timesheet_id"], "submit")
    error = await copy_entry(client, person, org, entry["id"], status=409, date=day(8))
    assert error["code"] == "TIME_LOCKED"
    # A closed job.
    await sql("UPDATE jobs SET status = 'completed' WHERE id = :i", i=job.id)
    error = await copy_entry(client, person, org, entry["id"], status=409, date=day(14))
    assert error["code"] == "JOB_CLOSED"
    for bad in ({}, {"date": "x"}, {"date": "1800-01-01"}, {"date": day(1), "extra": 1}):
        await copy_entry(client, person, org, entry["id"], status=422, **bad)


async def test_a_single_copy_access_rules(client: AsyncClient) -> None:
    _, org = await practice(client)
    _, other = await practice(client, "Other & Co")
    person = await staff(org)
    colleague = await staff(org)
    manager = await staff(org, "manager")
    viewer = await staff(org, "viewer")
    outsider = await staff(other)
    entry = await log_time(client, person, org)
    mine_elsewhere = await log_time(client, outsider, other)
    await copy_entry(client, colleague, org, entry["id"], status=403, date=day(1))
    await copy_entry(client, viewer, org, entry["id"], status=403, date=day(1))
    copy = await copy_entry(client, manager, org, entry["id"], date=day(1))
    assert copy["user_id"] == str(person.id) and copy["created_by"] == str(manager.id)
    await copy_entry(client, person, org, mine_elsewhere["id"], status=404, date=day(1))
    await copy_entry(client, outsider, org, entry["id"], status=404, date=day(1))
    unknown = await copy_entry(
        client, person, org, "00000000-0000-4000-8000-000000000000", status=404, date=day(1)
    )
    assert unknown["code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.parametrize("path", ["/time-entries/copy-week", "/time-entries/{id}/copy"])
async def test_copy_routes_need_a_signed_in_user(client: AsyncClient, path: str) -> None:
    _, org = await practice(client)
    url = org_url(org, path.format(id="00000000-0000-4000-8000-000000000000"))
    assert (await client.post(url, json={})).status_code == 401
