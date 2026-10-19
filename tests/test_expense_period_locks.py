"""Expenses and the period lock: every write that creates, edits, moves, deletes, submits or approves
dated expense lines asks the lock first (area ``expense``), so a closed period stays closed.
Returning, recalling, receipts and reads are not dated changes and never wait for it."""

import uuid
from datetime import date, timedelta

from app.services import period_locks
from tests.billing_helpers import count_rows
from tests.expense_helpers import (
    act,
    add_line,
    claim_url,
    create_claim,
    expense_team,
    line_body,
    line_row,
    patch_line,
    upload_receipt,
)
from tests.period_lock_helpers import assert_locked, set_lock, today
from tests.time_helpers import ok_data

DAY = timedelta(days=1)


async def post_claim(client, actor, org, *days: date):
    body = {
        "lines": [
            await line_body(org, incurred_on=str(day), description=f"Spent on {day}")
            for day in days
        ]
    }
    return await client.post(claim_url(org), headers=actor.headers, json=body)


async def post_line(client, actor, org, claim_id, day: date):
    return await client.post(
        claim_url(org, claim_id, "/lines"),
        headers=actor.headers,
        json=await line_body(org, incurred_on=str(day)),
    )


def line_on(claim: dict, day: date) -> str:
    return next(line["id"] for line in claim["lines"] if line["incurred_on"] == str(day))


async def scene(client):
    """A practice locked through ten days ago."""
    team = await expense_team(client)
    lock = today(team.org) - 10 * DAY
    await set_lock(team.org, lock)
    return team, lock


async def test_nothing_is_recorded_in_a_locked_period(client):
    team, lock = await scene(client)
    org = team.org
    staff = await team.member("staff")
    # Nobody is exempt, the Owner included; the boundary day itself is locked.
    for actor in (staff, team.owner):
        for day in (lock, lock - DAY, lock - 200 * DAY):
            assert_locked(await post_claim(client, actor, org, day), day, lock, "expense")
    assert await count_rows("SELECT count(*) FROM expense_claims") == 0
    # Of several dates the error names the earliest one, whatever the order.
    response = await post_claim(client, staff, org, lock + DAY, lock - 3 * DAY, lock - DAY)
    assert_locked(response, lock - 3 * DAY, lock, "expense")
    assert await count_rows("SELECT count(*) FROM expense_claims") == 0

    # The day after the lock is open, and so is every later one.
    created = await post_claim(client, staff, org, lock + DAY)
    assert created.status_code == 201
    cid = created.json()["data"]["id"]
    for day in (lock, lock - DAY):
        assert_locked(await post_line(client, staff, org, cid, day), day, lock, "expense")
    assert (await post_line(client, staff, org, cid, lock + 2 * DAY)).status_code == 201
    assert await count_rows("SELECT count(*) FROM expenses") == 2
    # A claim without lines has no dates to refuse.
    assert (await client.post(claim_url(org), headers=staff.headers, json={})).status_code == 201


async def test_locked_lines_cannot_be_edited_moved_or_deleted(client):
    team, lock = await scene(client)
    org = team.org
    staff = await team.member("staff")
    early, late = lock + 3 * DAY, lock + 6 * DAY
    claim = (await post_claim(client, staff, org, early, late)).json()["data"]
    cid = claim["id"]
    locked_line, open_line = line_on(claim, early), line_on(claim, late)
    new_lock = lock + 4 * DAY  # the period closes over the early line
    await set_lock(org, new_lock)

    def url(line_id):
        return claim_url(org, cid, f"/lines/{line_id}")

    response = await client.patch(
        url(locked_line), headers=staff.headers, json={"description": "x"}
    )
    assert_locked(response, early, new_lock, "expense")
    # Moving a locked line out of the period is refused, and so is moving one into it.
    response = await client.patch(
        url(locked_line), headers=staff.headers, json={"incurred_on": str(late)}
    )
    assert_locked(response, early, new_lock, "expense")
    inside = lock + 2 * DAY
    response = await client.patch(
        url(open_line), headers=staff.headers, json={"incurred_on": str(inside)}
    )
    assert_locked(response, inside, new_lock, "expense")
    assert_locked(
        await client.delete(url(locked_line), headers=staff.headers), early, new_lock, "expense"
    )
    # The claim as a whole is stuck with its locked line: it cannot go to approval or be deleted.
    assert_locked(
        await client.post(claim_url(org, cid, "/submit"), headers=staff.headers),
        early,
        new_lock,
        "expense",
    )
    assert_locked(
        await client.delete(claim_url(org, cid), headers=staff.headers), early, new_lock, "expense"
    )
    # Nothing moved; the open line is still free to change.
    detail = ok_data(await client.get(claim_url(org, cid), headers=staff.headers))
    assert {line["incurred_on"] for line in detail["lines"]} == {str(early), str(late)}
    assert {line["description"] for line in detail["lines"]} == {
        f"Spent on {early}",
        f"Spent on {late}",
    }
    await patch_line(client, staff, org, cid, open_line, description="Still editable")
    await patch_line(client, staff, org, cid, open_line, incurred_on=str(lock + 5 * DAY))
    deleted = await client.delete(url(open_line), headers=staff.headers)
    assert deleted.status_code == 200
    assert await count_rows("SELECT count(*) FROM expenses") == 1


async def test_a_lock_set_after_submission_stops_the_approval(client):
    team, lock = await scene(client)
    org, owner = team.org, team.owner
    staff = await team.member("staff")
    day, free = lock + 3 * DAY, lock + 8 * DAY
    stuck = (await post_claim(client, staff, org, day)).json()["data"]["id"]
    fine = (await post_claim(client, staff, org, free)).json()["data"]["id"]
    for claim_id in (stuck, fine):
        await act(client, staff, org, claim_id, "submit")
    new_lock = lock + 4 * DAY
    await set_lock(org, new_lock)

    refused = await client.post(claim_url(org, stuck, "/approve"), headers=owner.headers)
    assert_locked(refused, day, new_lock, "expense")
    # A bulk approval is all or nothing: the open claim waits with the locked one.
    bulk = await client.post(
        team.url("/expense-approvals/bulk-approve"),
        headers=owner.headers,
        json={"claim_ids": [fine, stuck]},
    )
    assert_locked(bulk, day, new_lock, "expense")
    states = await count_rows("SELECT count(*) FROM expense_claims WHERE status = 'submitted'")
    assert states == 2

    # Returning and recalling are decisions, not changes to dated data: the lock does not stop them.
    returned = await act(client, owner, org, stuck, "return", json={"reason": "Too late"})
    assert returned["status"] == "returned"
    # ... but sending it again does, and the claim stays with its claimant.
    assert_locked(
        await client.post(claim_url(org, stuck, "/submit"), headers=staff.headers),
        day,
        new_lock,
        "expense",
    )
    assert (await act(client, staff, org, fine, "recall"))["status"] == "draft"
    assert (await act(client, staff, org, fine, "submit"))["status"] == "submitted"
    assert (await act(client, owner, org, fine, "approve"))["status"] == "approved"

    # An approved claim's lines can be corrected while their dates are open, not once they close.
    line_id = ok_data(await client.get(claim_url(org, fine), headers=owner.headers))["lines"][0][
        "id"
    ]
    fixed = await patch_line(client, owner, org, fine, line_id, net=1500, reason="Fix")
    assert fixed["net"] == 1500
    closing = free + DAY
    await set_lock(org, closing)
    response = await client.patch(
        claim_url(org, fine, f"/lines/{line_id}"),
        headers=owner.headers,
        json={"net": 1600, "reason": "Again"},
    )
    assert_locked(response, free, closing, "expense")
    assert (await line_row(line_id))["net"] == 1500


async def test_receipts_and_reads_carry_on_in_a_locked_period(client):
    team, lock = await scene(client)
    org = team.org
    staff = await team.member("staff")
    day = lock + 3 * DAY
    claim = (await post_claim(client, staff, org, day)).json()["data"]
    cid, lid = claim["id"], claim["lines"][0]["id"]
    await set_lock(org, lock + 5 * DAY)
    # A receipt proves what was spent; it does not change the dated line.
    uploaded = await upload_receipt(client, staff, org, lid)
    assert uploaded.status_code == 201
    receipt_id = uploaded.json()["data"]["id"]
    detail = await client.get(claim_url(org, cid), headers=staff.headers)
    assert detail.status_code == 200 and detail.json()["data"]["lines"][0]["receipt_count"] == 1
    listed = await client.get(claim_url(org), headers=staff.headers)
    assert listed.status_code == 200 and len(listed.json()["data"]) == 1
    lines = await client.get(team.url("/expenses"), headers=staff.headers)
    assert lines.status_code == 200 and len(lines.json()["data"]) == 1
    removed = await client.delete(
        team.url(f"/expenses/{lid}/receipts/{receipt_id}"), headers=staff.headers
    )
    assert removed.status_code == 200


async def test_every_dated_write_asks_the_period_lock_with_its_dates(client, monkeypatch):
    team = await expense_team(client)
    org, owner = team.org, team.owner
    org_id = uuid.UUID(org["id"])
    staff = await team.member("staff")
    d1, d2, d3 = date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9)
    calls: list[tuple[str, frozenset[date], str, uuid.UUID]] = []
    real_one, real_many = period_locks.assert_open, period_locks.assert_open_many

    async def one(session, organization_id, on_date, area):
        calls.append(("one", frozenset({on_date}), area, organization_id))
        await real_one(session, organization_id, on_date, area)

    async def many(session, organization_id, days, area):
        calls.append(("many", frozenset(days), area, organization_id))
        await real_many(session, organization_id, days, area)

    monkeypatch.setattr(period_locks, "assert_open", one)
    monkeypatch.setattr(period_locks, "assert_open_many", many)

    def taken() -> list[tuple[str, frozenset[date], str, uuid.UUID]]:
        seen = list(calls)
        calls.clear()
        return seen

    def asked(kind: str, *days: date) -> list[tuple[str, frozenset[date], str, uuid.UUID]]:
        return [(kind, frozenset(days), "expense", org_id)]

    # Creating: a claim with lines asks once for all their dates; one without asks nothing.
    await create_claim(client, staff, org)
    assert taken() == []
    created = await post_claim(client, staff, org, d1, d2)
    assert created.status_code == 201
    claim = created.json()["data"]
    cid, first, second = claim["id"], line_on(claim, d1), line_on(claim, d2)
    assert taken() == asked("many", d1, d2)

    # Lines: add, edit (the old and the new date), move, delete; an unchanged edit asks nothing.
    extra = await add_line(client, staff, org, cid, incurred_on=str(d1))
    assert taken() == asked("one", d1)
    await patch_line(client, staff, org, cid, extra["id"], description="Edited")
    assert taken() == asked("many", d1)
    await patch_line(client, staff, org, cid, extra["id"], description="Edited")
    assert taken() == []
    await patch_line(client, staff, org, cid, first, incurred_on=str(d3))
    assert taken() == asked("many", d1, d3)
    deleted = await client.delete(
        claim_url(org, cid, f"/lines/{extra['id']}"), headers=staff.headers
    )
    assert deleted.status_code == 200
    assert taken() == asked("one", d1)

    # The workflow: submit and approve ask for the dates of the lines; return and recall do not.
    assert (await act(client, staff, org, cid, "submit"))["status"] == "submitted"
    assert taken() == asked("many", d2, d3)
    assert (await act(client, owner, org, cid, "return", json={"reason": "Redo"}))["status"] == (
        "returned"
    )
    assert taken() == []
    assert (await act(client, staff, org, cid, "submit"))["status"] == "submitted"
    assert taken() == asked("many", d2, d3)
    assert (await act(client, staff, org, cid, "recall"))["status"] == "draft"
    assert taken() == []
    await act(client, staff, org, cid, "submit")
    taken()
    assert (await act(client, owner, org, cid, "approve"))["status"] == "approved"
    assert taken() == asked("many", d2, d3)

    # A correction asks for the old and the new date.
    await patch_line(client, owner, org, cid, second, net=1500, reason="Fix")
    assert taken() == asked("many", d2)
    await patch_line(client, owner, org, cid, second, incurred_on=str(d1), reason="Fix the date")
    assert taken() == asked("many", d1, d2)

    # Bulk approval asks once per claim.
    one_claim = (await post_claim(client, staff, org, d1)).json()["data"]["id"]
    other_claim = (await post_claim(client, staff, org, d2, d3)).json()["data"]["id"]
    taken()
    for claim_id in (one_claim, other_claim):
        await act(client, staff, org, claim_id, "submit")
    taken()
    bulk = await client.post(
        team.url("/expense-approvals/bulk-approve"),
        headers=owner.headers,
        json={"claim_ids": [one_claim, other_claim]},
    )
    assert bulk.status_code == 200
    assert sorted(taken(), key=lambda call: sorted(call[1])) == sorted(
        asked("many", d1) + asked("many", d2, d3), key=lambda call: sorted(call[1])
    )

    # Receipts, reads and the listings never ask.
    draft = (await post_claim(client, staff, org, d1)).json()["data"]
    taken()
    assert (await upload_receipt(client, staff, org, draft["lines"][0]["id"])).status_code == 201
    for path in (
        "/expense-claims",
        "/expenses",
        f"/expense-claims/{draft['id']}",
        "/expense-approvals",
    ):
        assert (await client.get(team.url(path), headers=owner.headers)).status_code == 200
    assert taken() == []

    # Deleting a draft asks for its dates; deleting an empty one asks nothing.
    gone = await client.delete(claim_url(org, draft["id"]), headers=staff.headers)
    assert gone.status_code == 200
    assert taken() == asked("many", d1)
    empty = await create_claim(client, staff, org)
    taken()
    assert (
        await client.delete(claim_url(org, empty["id"]), headers=staff.headers)
    ).status_code == 200
    assert taken() == []
