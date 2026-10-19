"""Corrections of approved, unbilled time: reasons, rules, history, access and immutability."""

import asyncio
import itertools
import json
import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError

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
    approve_sheet,
    approved_entry,
    correct,
    day,
    get_entry,
    get_sheet,
    history,
    history_rows,
    sheet_action,
)
from tests.time_helpers import log_time, org_url, set_entry, set_manager, set_practice


async def team(client: AsyncClient):
    """A practice with its owner, a person on 100 pounds an hour, their approver and a client."""
    owner, org = await practice(client)
    person = await staff(org)
    approver = await staff(org, "manager")
    acme = await make_client(org, "Acme Ltd")
    await make_staff_rate(org, person, 10_000, 4_000)
    return owner, org, person, approver, acme


async def custom_staff(client: AsyncClient, owner, org: dict, name: str, *codes: str):
    response = await client.post(
        org_url(org, "/roles"),
        headers=owner.headers,
        json={"name": name, "permissions": list(codes)},
    )
    assert response.status_code == 201, response.text
    return await staff(org, name)


# --- The correction -------------------------------------------------------------------------


async def test_a_correction_keeps_the_rate_and_writes_one_history_row(client: AsyncClient) -> None:
    _, org, person, approver, acme = await team(client)
    entry = await approved_entry(
        client, org, person, approver, client_id=acme.id, minutes=60, description="Draft",
        internal_note="First note",
    )  # fmt: skip
    assert entry["timesheet_status"] == "approved" and entry["is_locked"] is True
    # The person's rate goes up after the time was approved: a correction of the hours keeps
    # the rate the entry was saved with.
    await make_staff_rate(org, person, 15_000, 6_000, effective_from=date(2026, 1, 1))

    corrected = await correct(
        client, approver, org, entry["id"], reason="Hours were mis-keyed", minutes=90
    )
    assert corrected["minutes"] == 90
    assert corrected["charge_rate"] == 10_000 and corrected["rate_source"] == entry["rate_source"]
    assert corrected["charge_value"] == time_value(90, 10_000) == 15_000
    assert (corrected["cost_rate"], corrected["cost_value"]) == (4_000, 6_000)
    assert corrected["timesheet_status"] == "approved" and corrected["billing_status"] == "unbilled"
    assert corrected["internal_note"] == "First note" and corrected["is_locked"] is True

    [row] = await history_rows(org, entry["id"])
    assert row["reason"] == "Hours were mis-keyed"
    assert str(row["user_id"]) == str(person.id) and str(row["corrected_by"]) == str(approver.id)
    assert (row["before"]["minutes"], row["after"]["minutes"]) == (60, 90)
    assert (row["before"]["charge_value"], row["after"]["charge_value"]) == (10_000, 15_000)
    assert row["before"]["client_name"] == row["after"]["client_name"] == acme.name
    assert row["before"]["entry_date"] == row["after"]["entry_date"] == day(0)
    assert row["before"]["week_start"] == row["after"]["week_start"] == day(0)
    assert row["before"]["internal_note"] == "First note"

    [shown] = await history(client, approver, org, entry["id"])
    assert shown["id"] == str(row["id"]) and shown["time_entry_id"] == entry["id"]
    assert shown["user_name"] == person.full_name
    assert shown["corrected_by_name"] == approver.full_name
    assert shown["reason"] == "Hours were mis-keyed" and shown["created_at"]
    assert set(shown["changes"]) == {"minutes", "charge_value", "cost_value"}
    assert shown["changes"]["minutes"] == [60, 90]

    # One audit entry, with the reason and the history row.
    [logged] = await audit_entries(org["id"], "time_entry.corrected")
    assert logged["actor_user_id"] == str(approver.id) and logged["target_id"] == entry["id"]
    assert logged["metadata"]["reason"] == "Hours were mis-keyed"
    assert logged["metadata"]["history_id"] == str(row["id"])
    assert logged["metadata"]["changes"]["minutes"] == [60, 90]
    # The sheet stays approved and its totals follow.
    sheet = await get_sheet(client, approver, org, entry["timesheet_id"])
    assert sheet["status"] == "approved" and sheet["total_minutes"] == 90
    assert sheet["billable_value"] == 15_000

    # A correction of the description alone keeps the rate too; newest first.
    again = await correct(client, approver, org, entry["id"], reason="Typo", description="Final")
    assert again["charge_rate"] == 10_000 and again["minutes"] == 90
    listed = await history(client, approver, org, entry["id"])
    assert [h["reason"] for h in listed] == ["Typo", "Hours were mis-keyed"]
    assert listed[0]["changes"] == {"description": ["Draft", "Final"]}
    assert len(await history_rows(org, entry["id"])) == 2


async def test_a_new_day_or_job_takes_the_rate_again(client: AsyncClient) -> None:
    _, org, person, approver, acme = await team(client)
    dear = await make_job(org, acme, "Dear job", hourly_rate=20_000)
    entry = await approved_entry(client, org, person, approver, client_id=acme.id, minutes=60)
    await make_staff_rate(org, person, 15_000, 6_000, effective_from=date(2026, 1, 1))

    moved = await correct(client, approver, org, entry["id"], reason="Wrong day", date=day(1))
    assert moved["entry_date"] == day(1) and moved["timesheet_id"] == entry["timesheet_id"]
    assert (moved["charge_rate"], moved["cost_rate"]) == (15_000, 6_000)
    assert (moved["charge_value"], moved["cost_value"]) == (15_000, 6_000)
    [shown] = await history(client, approver, org, entry["id"])
    assert set(shown["changes"]) == {
        "entry_date", "charge_rate", "charge_value", "cost_rate", "cost_value"
    }  # fmt: skip
    assert shown["changes"]["entry_date"] == [day(0), day(1)]

    # A job with its own hourly rate prices the entry; the client follows the job.
    rejobbed = await correct(client, approver, org, entry["id"], reason="Wrong job", job_id=dear.id)
    assert rejobbed["job_id"] == str(dear.id) and rejobbed["client_id"] == str(acme.id)
    assert (rejobbed["charge_rate"], rejobbed["rate_source"]) == (20_000, "job")
    assert rejobbed["job_name"] == "Dear job"


async def test_a_correction_can_move_time_to_another_week(client: AsyncClient) -> None:
    _, org, person, approver, acme = await team(client)
    entry = await log_time(client, person, org, entry_date=day(0), client_id=acme.id, minutes=60)
    kept = await log_time(client, person, org, entry_date=day(1), client_id=acme.id, minutes=30)
    await approve_sheet(client, org, person, approver, entry["timesheet_id"])
    assert (await get_sheet(client, approver, org, entry["timesheet_id"]))["total_minutes"] == 90

    moved = await correct(client, approver, org, entry["id"], reason="Wrong week", date=day(8))
    assert moved["entry_date"] == day(8) and moved["timesheet_id"] != entry["timesheet_id"]
    # The entry joined that week's timesheet, which was created open: it follows that sheet
    # from now on (an open sheet is edited, not corrected).
    assert moved["timesheet_status"] == "open"
    first_week = await get_sheet(client, approver, org, entry["timesheet_id"])
    second_week = await get_sheet(client, approver, org, moved["timesheet_id"])
    assert (first_week["total_minutes"], first_week["entry_count"], first_week["status"]) == (
        30,
        1,
        "approved",
    )
    assert (second_week["total_minutes"], second_week["entry_count"]) == (60, 1)
    assert second_week["week_start"] == day(7)
    [row] = await history_rows(org, entry["id"])
    assert (row["before"]["week_start"], row["after"]["week_start"]) == (day(0), day(7))
    assert row["before"]["timesheet_id"] == entry["timesheet_id"]
    assert row["after"]["timesheet_id"] == moved["timesheet_id"]

    # Into a week that is approved too: the entry joins it, and the totals of both follow.
    other = await log_time(client, person, org, entry_date=day(15), client_id=acme.id, minutes=24)
    await approve_sheet(client, org, person, approver, other["timesheet_id"])
    back = await correct(client, approver, org, kept["id"], reason="Not that week", date=day(16))
    assert back["timesheet_id"] == other["timesheet_id"] and back["timesheet_status"] == "approved"
    third_week = await get_sheet(client, approver, org, other["timesheet_id"])
    assert (third_week["total_minutes"], third_week["entry_count"]) == (54, 2)
    first_week = await get_sheet(client, approver, org, entry["timesheet_id"])
    assert (first_week["total_minutes"], first_week["entry_count"]) == (0, 0)


async def test_only_approved_unbilled_time_can_be_corrected(client: AsyncClient) -> None:
    _, org, person, approver, acme = await team(client)
    entry = await log_time(client, person, org, client_id=acme.id)
    sheet_id = entry["timesheet_id"]

    async def refused(**details: str) -> None:
        error = await correct(
            client, approver, org, entry["id"], status=409, reason="Fix", minutes=90
        )
        assert error["code"] == "TIME_NOT_CORRECTABLE"
        for key, value in details.items():
            assert error["details"][key] == value
        assert await history_rows(org, entry["id"]) == []

    await refused(timesheet_status="open")  # an open sheet is edited, not corrected
    await sheet_action(client, person, org, sheet_id, "submit")
    await refused(timesheet_status="submitted")  # a submitted one is returned with a reason
    await sheet_action(client, approver, org, sheet_id, "reject", json={"decision_note": "Detail"})
    await refused(timesheet_status="rejected")
    await sheet_action(client, person, org, sheet_id, "submit")
    await sheet_action(client, approver, org, sheet_id, "approve")
    for billing_status in ("in_draft", "billed", "written_off"):
        await set_entry(entry["id"], billing_status=billing_status)
        await refused(timesheet_status="approved", billing_status=billing_status)
    await set_entry(entry["id"], billing_status="unbilled")
    fixed = await correct(client, approver, org, entry["id"], reason="Fix", minutes=90)
    assert fixed["minutes"] == 90


async def test_a_reason_is_required(client: AsyncClient) -> None:
    _, org, person, approver, acme = await team(client)
    entry = await approved_entry(client, org, person, approver, client_id=acme.id)
    for body in ({}, {"reason": None}, {"reason": ""}, {"reason": "   "}):
        error = await correct(client, approver, org, entry["id"], status=422, minutes=90, **body)
        assert error["code"] == "TIME_CORRECTION_REASON_REQUIRED"
        assert error["details"][0]["field"] == "reason"
    too_long = await correct(
        client, approver, org, entry["id"], status=422, reason="r" * 2001, minutes=90
    )
    assert too_long["details"][0]["field"] == "reason"
    assert await history_rows(org, entry["id"]) == []
    assert (await get_entry(client, approver, org, entry["id"]))["minutes"] == 60
    longest = await correct(client, approver, org, entry["id"], reason="r" * 2000, minutes=90)
    assert longest["minutes"] == 90


async def test_a_correction_must_change_something_and_be_valid(client: AsyncClient) -> None:
    _, org, person, approver, acme = await team(client)
    entry = await approved_entry(client, org, person, approver, client_id=acme.id, minutes=60)
    for body in ({}, {"minutes": 60}, {"description": None}, {"date": day(0)}):
        error = await correct(client, approver, org, entry["id"], status=422, reason="Why", **body)
        assert error["code"] == "TIME_CORRECTION_EMPTY"
    for body in (
        {"minutes": 0},
        {"minutes": 1441},
        {"minutes": None},
        {"date": None},
        {"date": "x"},
        {"nonsense": 1},
        {"internal_note": "n" * 2001},
    ):
        error = await correct(client, approver, org, entry["id"], status=422, reason="Why", **body)
        assert error["code"] == "VALIDATION_ERROR", body
    assert await history_rows(org, entry["id"]) == []
    # The names of an edit work as well as the short ones.
    same = await correct(
        client, approver, org, entry["id"], reason="Why", entry_date=day(2), is_billable=False
    )
    assert same["entry_date"] == day(2) and same["is_billable"] is False
    noted = await correct(client, approver, org, entry["id"], reason="Why", internal_note="Note")
    assert noted["internal_note"] == "Note"
    cleared = await correct(client, approver, org, entry["id"], reason="Why", internal_note="  ")
    assert cleared["internal_note"] is None


# --- The rules of an edit -------------------------------------------------------------------


async def test_corrected_minutes_follow_the_practice_increment(client: AsyncClient) -> None:
    _, org, person, approver, acme = await team(client)
    entry = await approved_entry(client, org, person, approver, client_id=acme.id, minutes=60)
    await set_practice(org, time_increment_minutes=15)
    for given, expected in ((70, 75), (76, 90), (1, 15)):
        await correct(client, approver, org, entry["id"], reason="Round", minutes=given)
        assert (await get_entry(client, approver, org, entry["id"]))["minutes"] == expected
    assert (await get_entry(client, approver, org, entry["id"]))["charge_value"] == time_value(
        15, 10_000
    )


async def test_a_correction_cannot_make_a_day_longer_than_24_hours(client: AsyncClient) -> None:
    _, org, person, approver, acme = await team(client)
    entry = await log_time(client, person, org, entry_date=day(0), client_id=acme.id, minutes=60)
    await log_time(client, person, org, entry_date=day(1), client_id=acme.id, minutes=1380)
    await approve_sheet(client, org, person, approver, entry["timesheet_id"])
    error = await correct(
        client, approver, org, entry["id"], status=422, reason="Fix", date=day(1), minutes=66
    )
    assert error["code"] == "DAY_LIMIT_EXCEEDED"
    assert await history_rows(org, entry["id"]) == []
    # Exactly 24 hours is fine.
    moved = await correct(client, approver, org, entry["id"], reason="Fix", date=day(1))
    assert moved["entry_date"] == day(1)


async def test_a_correction_checks_its_references(client: AsyncClient) -> None:
    _, org, person, approver, acme = await team(client)
    entry = await approved_entry(client, org, person, approver, client_id=acme.id, minutes=60)
    done = await make_job(org, acme, "Done job", status="completed")
    quiet = await make_client(org, "Quiet Ltd", status="inactive")
    other_client = await make_client(org, "Other Ltd")
    their_job = await make_job(org, other_client)
    internal = await make_job(org, acme, "Internal", billing_basis="non_billable")
    _, elsewhere = await practice(client, "Other & Co")
    foreign_client = await make_client(elsewhere)
    foreign_code = await activity_code_id(elsewhere, "ACC")

    async def refused(status: int, **body) -> dict:
        error = await correct(
            client, approver, org, entry["id"], status=status, reason="Fix", **body
        )
        assert await history_rows(org, entry["id"]) == []
        return error

    assert (await refused(409, job_id=done.id))["code"] == "JOB_CLOSED"
    assert (await refused(409, client_id=quiet.id))["code"] == "CLIENT_INACTIVE"
    mismatch = await refused(422, client_id=acme.id, job_id=their_job.id)
    assert mismatch["details"][0]["field"] == "job_id"
    for body in (
        {"client_id": foreign_client.id},
        {"activity_code_id": foreign_code},
        {"job_id": uuid.uuid4()},
        {"client_id": uuid.uuid4()},
    ):
        assert (await refused(404, **body))["code"] == "RESOURCE_NOT_FOUND"
    forced = await refused(422, job_id=internal.id, billable=True)
    assert forced["details"][0]["field"] == "is_billable"
    no_client = await refused(422, client_id=None, billable=True)
    assert no_client["details"][0]["field"] == "is_billable"
    assert (await get_entry(client, approver, org, entry["id"]))["minutes"] == 60

    # Without a client the time is not billable; the correction says so.
    internal_time = await correct(client, approver, org, entry["id"], reason="Fix", client_id=None)
    assert internal_time["client_id"] is None and internal_time["is_billable"] is False


async def test_a_description_can_be_corrected_on_time_of_a_closed_job(client: AsyncClient) -> None:
    _, org, person, approver, acme = await team(client)
    job = await make_job(org, acme)
    entry = await approved_entry(client, org, person, approver, job_id=job.id, minutes=60)
    await sql("UPDATE jobs SET status = 'completed' WHERE id = :i", i=job.id)
    fixed = await correct(client, approver, org, entry["id"], reason="Detail", description="More")
    assert fixed["description"] == "More"
    error = await correct(client, approver, org, entry["id"], status=409, reason="Fix", minutes=90)
    assert error["code"] == "JOB_CLOSED"


async def test_a_correction_is_refused_in_a_locked_period(client: AsyncClient) -> None:
    _, org, person, approver, acme = await team(client)
    entry = await log_time(client, person, org, client_id=acme.id, entry_date=day(0))
    free = await log_time(client, person, org, client_id=acme.id, entry_date=day(1))
    await approve_sheet(client, org, person, approver, entry["timesheet_id"])
    await set_lock(org, date.fromisoformat(day(0)))
    # Time dated in the locked period cannot change, and nothing can move into it.
    error = await correct(client, approver, org, entry["id"], status=409, reason="Fix", minutes=90)
    assert error["code"] == "PERIOD_LOCKED"
    assert error["details"] == {"date": day(0), "locked_through": day(0), "area": "time"}
    error = await correct(client, approver, org, free["id"], status=409, reason="Fix", date=day(0))
    assert error["code"] == "PERIOD_LOCKED" and error["details"]["date"] == day(0)
    assert await history_rows(org, entry["id"]) == [] and await history_rows(org, free["id"]) == []
    assert (await get_entry(client, approver, org, free["id"]))["entry_date"] == day(1)
    fine = await correct(client, approver, org, free["id"], reason="Fine", minutes=90)
    assert fine["minutes"] == 90
    # Reopened, the locked entry can be corrected too.
    await set_lock(org, None)
    assert (await correct(client, approver, org, entry["id"], reason="Fix", minutes=90))[
        "minutes"
    ] == 90


# --- Who may correct ------------------------------------------------------------------------


async def test_who_may_correct_approved_time(client: AsyncClient) -> None:
    owner, org, person, approver, acme = await team(client)
    _, other = await practice(client, "Other & Co")
    outsider = await staff(other, "manager")
    their_entry = await approved_entry(
        client, other, await staff(other), outsider, client_id=(await make_client(other)).id
    )
    colleague = await staff(org)
    viewer = await staff(org, "viewer")
    billing = await staff(org, "billing")
    approve_only = await custom_staff(client, owner, org, "Approver", "timesheet.approve")
    manage_only = await custom_staff(client, owner, org, "Time admin", "time.manage_all")
    entry = await approved_entry(client, org, person, approver, client_id=acme.id)
    for actor in (person, colleague, viewer, billing):
        error = await correct(client, actor, org, entry["id"], status=403, reason="Fix", minutes=90)
        assert error["code"] == "PERMISSION_DENIED"
    assert (await get_entry(client, approver, org, entry["id"]))["minutes"] == 60
    # Time.manage_all or timesheet.approve: either will do.
    for actor, reason, minutes in (
        (approve_only, "A", 90),
        (manage_only, "B", 120),
        (owner, "C", 60),
    ):
        corrected = await correct(client, actor, org, entry["id"], reason=reason, minutes=minutes)
        assert corrected["minutes"] == minutes
    assert len(await history_rows(org, entry["id"])) == 3
    # Another practice's entry is unknown here, and its people cannot reach this practice.
    unknown = await correct(
        client, owner, org, their_entry["id"], status=404, reason="Fix", minutes=90
    )
    assert unknown["code"] == "RESOURCE_NOT_FOUND"
    unreachable = await correct(
        client, outsider, org, entry["id"], status=404, reason="Fix", minutes=90
    )
    assert unreachable["code"] == "RESOURCE_NOT_FOUND"


async def test_nobody_corrects_their_own_approved_time_without_organization_update(
    client: AsyncClient,
) -> None:
    owner, org, _, approver, acme = await team(client)
    other_approver = await staff(org, "manager")
    mine = await log_time(client, approver, org, client_id=acme.id)
    await sheet_action(client, approver, org, mine["timesheet_id"], "submit")
    await sheet_action(client, other_approver, org, mine["timesheet_id"], "approve")
    error = await correct(client, approver, org, mine["id"], status=403, reason="Fix", minutes=90)
    assert error["code"] == "SELF_CORRECTION_FORBIDDEN"
    # Someone else can; so can an Owner or Admin (organization.update), like self-approval.
    by_other = await correct(client, other_approver, org, mine["id"], reason="Fix", minutes=90)
    assert by_other["minutes"] == 90
    own = await approved_entry(client, org, owner, owner, client_id=acme.id)
    assert (await correct(client, owner, org, own["id"], reason="Fix", minutes=90))["minutes"] == 90


# --- The history ----------------------------------------------------------------------------


async def test_who_reads_the_history_and_what_they_see_of_it(client: AsyncClient) -> None:
    owner, org, person, approver, acme = await team(client)
    _, other = await practice(client, "Other & Co")
    outsider = await staff(other)
    lead = await staff(org)
    stranger = await staff(org)
    viewer = await staff(org, "viewer")
    await set_manager(org, person, lead)
    entry = await approved_entry(
        client, org, person, approver, client_id=acme.id, minutes=60, internal_note="Old note"
    )
    await correct(
        client, approver, org, entry["id"], reason="One", minutes=90, internal_note="New note"
    )
    await correct(client, approver, org, entry["id"], reason="Two", description="Words")

    for actor in (person, lead, viewer, owner, approver):
        rows = await history(client, actor, org, entry["id"])
        assert [r["reason"] for r in rows] == ["Two", "One"]
    await history(client, stranger, org, entry["id"], status=403)
    await history(client, outsider, org, entry["id"], status=404)
    await history(client, owner, org, "00000000-0000-4000-8000-000000000000", status=404)

    # Cost figures need rate.read; the note belongs to the owner and those who read all time.
    own = (await history(client, person, org, entry["id"]))[1]
    assert own["before"]["cost_rate"] is None and own["after"]["cost_value"] is None
    assert "cost_value" not in own["changes"] and "cost_rate" not in own["changes"]
    assert own["changes"]["internal_note"] == ["Old note", "New note"]
    seen = (await history(client, approver, org, entry["id"]))[1]
    assert (seen["before"]["cost_rate"], seen["after"]["cost_value"]) == (4_000, 6_000)
    assert seen["changes"]["cost_value"] == [4_000, 6_000]
    hidden = (await history(client, lead, org, entry["id"]))[1]
    assert hidden["before"]["internal_note"] is None and hidden["after"]["internal_note"] is None
    assert "internal_note" not in hidden["changes"]
    assert hidden["changes"]["minutes"] == [60, 90]
    # The audit trail says that the note changed, not what it said.
    [first, _] = await audit_entries(org["id"], "time_entry.corrected")
    assert first["metadata"]["changes"]["internal_note"] == [True, True]
    assert "Old note" not in json.dumps(await audit_entries(org["id"]))


async def test_history_is_paginated_and_survives_the_entry(client: AsyncClient) -> None:
    owner, org, person, approver, acme = await team(client)
    entry = await approved_entry(client, org, person, approver, client_id=acme.id, minutes=60)
    for index, minutes in enumerate((66, 72, 78)):
        await correct(client, approver, org, entry["id"], reason=f"Step {index}", minutes=minutes)
    url = org_url(org, f"/time-entries/{entry['id']}/history")
    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = (await client.get(url, headers=approver.headers, params=params)).json()
        seen += [row["reason"] for row in body["data"]]
        cursor = body["meta"]["next_cursor"]
        if not cursor:
            break
    assert seen == ["Step 2", "Step 1", "Step 0"]

    # Reopening the sheet lets the person delete the entry; the history stays readable.
    await sheet_action(client, approver, org, entry["timesheet_id"], "reopen")
    deleted = await client.delete(
        org_url(org, f"/time-entries/{entry['id']}"), headers=person.headers
    )
    assert deleted.status_code == 200, deleted.text
    assert len(await history(client, person, org, entry["id"])) == 3
    assert len(await history(client, owner, org, entry["id"])) == 3
    assert len(await history_rows(org, entry["id"])) == 3
    await history(client, await staff(org), org, entry["id"], status=403)


async def test_corrections_at_the_same_time_form_one_chain(client: AsyncClient) -> None:
    _, org, person, approver, acme = await team(client)
    entry = await approved_entry(client, org, person, approver, client_id=acme.id, minutes=60)
    results = await asyncio.gather(
        *(
            correct(client, approver, org, entry["id"], reason=f"Change {m}", minutes=m)
            for m in (66, 72, 78)
        )
    )
    assert sorted(r["minutes"] for r in results) == [66, 72, 78]
    rows = await history_rows(org, entry["id"])
    assert len(rows) == 3 and rows[0]["before"]["minutes"] == 60
    # Each correction started from where the one before it left the entry.
    for earlier, later in itertools.pairwise(rows):
        assert later["before"]["minutes"] == earlier["after"]["minutes"]


# --- Immutability ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE time_entry_history SET reason = 'Changed'",
        "UPDATE time_entry_history SET after = '{}'",
        "UPDATE time_entry_history SET corrected_by = gen_random_uuid()",
        "DELETE FROM time_entry_history",
    ],
)
async def test_history_rows_cannot_be_changed_or_deleted(
    client: AsyncClient, statement: str
) -> None:
    _, org, person, approver, acme = await team(client)
    entry = await approved_entry(client, org, person, approver, client_id=acme.id)
    await correct(client, approver, org, entry["id"], reason="Original reason", minutes=90)
    with pytest.raises(IntegrityError, match="append-only"):
        await sql(statement)
    [row] = await history_rows(org, entry["id"])
    assert row["reason"] == "Original reason" and row["after"]["minutes"] == 90
    # Appending is what the table is for.
    await sql(
        "INSERT INTO time_entry_history (id, organization_id, time_entry_id, user_id, "
        "corrected_by, reason, before, after) "
        "VALUES (gen_random_uuid(), :o, :e, :u, :c, 'Direct', '{}', '{}')",
        o=org["id"], e=str(entry["id"]), u=str(person.id), c=str(approver.id),
    )  # fmt: skip
    assert len(await history_rows(org, entry["id"])) == 2


async def test_the_history_goes_with_the_practice(client: AsyncClient) -> None:
    _, org, person, approver, acme = await team(client)
    entry = await approved_entry(client, org, person, approver, client_id=acme.id)
    await correct(client, approver, org, entry["id"], reason="Original reason", minutes=90)
    assert len(await history_rows(org, entry["id"])) == 1
    # Only the practice's own deletion removes the rows: the cascade is not a direct DELETE.
    await sql("DELETE FROM organizations WHERE id = :o", o=org["id"])
    assert await history_rows(org, entry["id"]) == []


@pytest.mark.parametrize(
    ("reason", "before", "after", "constraint"),
    [
        ("   ", "{}", "{}", "ck_time_entry_history_reason_length"),
        ("r" * 2001, "{}", "{}", "ck_time_entry_history_reason_length"),
        ("Because", "[]", "{}", "ck_time_entry_history_snapshots_are_objects"),
        ("Because", "{}", "1", "ck_time_entry_history_snapshots_are_objects"),
    ],
)
async def test_the_history_table_refuses_bad_rows(
    client: AsyncClient, reason: str, before: str, after: str, constraint: str
) -> None:
    _, org, person, approver, acme = await team(client)
    entry = await approved_entry(client, org, person, approver, client_id=acme.id)
    with pytest.raises(IntegrityError, match=constraint):
        await sql(
            "INSERT INTO time_entry_history (id, organization_id, time_entry_id, user_id, "
            "corrected_by, reason, before, after) VALUES (gen_random_uuid(), :o, :e, :u, :c, :r, "
            "CAST(:b AS jsonb), CAST(:a AS jsonb))",
            o=org["id"], e=str(entry["id"]), u=str(person.id), c=str(approver.id),
            r=reason, b=before, a=after,
        )  # fmt: skip
