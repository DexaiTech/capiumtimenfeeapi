"""Expenses under concurrency: every transition and every receipt change locks the claim row, so two
requests at once give one winner and one clean refusal, never two decisions or a line on a claim
that has already gone for approval."""

import asyncio

from tests.billing_helpers import count_rows
from tests.clients_helpers import scalar, sql
from tests.expense_helpers import (
    act,
    claim_url,
    create_claim,
    expense_team,
    line_body,
    receipt_rows,
    receipt_url,
    upload_receipt,
)
from tests.factories import audit_entries, error
from tests.notification_helpers import inbox
from tests.ticket_helpers import stored_files
from tests.time_helpers import ok_data


async def submitted_claim(client, team, staff, lines: int = 1) -> str:
    claim = await create_claim(client, staff, team.org, lines=lines)
    await act(client, staff, team.org, claim["id"], "submit")
    return claim["id"]


async def events_of(claim_id: str) -> list[str]:
    rows = await sql(
        "SELECT action FROM expense_claim_events WHERE claim_id = :c ORDER BY created_at, id",
        c=claim_id,
    )
    return [row["action"] for row in rows]


async def test_two_approvals_at_once_give_one_decision(client):
    team = await expense_team(client)
    org = team.org
    staff, admin = await team.member("staff"), await team.member("admin")
    cid = await submitted_claim(client, team, staff)
    responses = await asyncio.gather(
        client.post(claim_url(org, cid, "/approve"), headers=team.owner.headers),
        client.post(claim_url(org, cid, "/approve"), headers=admin.headers),
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    loser = next(r for r in responses if r.status_code == 409)
    assert error(loser)["code"] == "INVALID_STATUS_TRANSITION"
    winner = next(r for r in responses if r.status_code == 200).json()["data"]["decided_by"]
    assert await events_of(cid) == ["submitted", "approved"]
    [entry] = await audit_entries(team.id, "expense_claim.approved")
    assert entry["actor_user_id"] == winner
    assert (
        await scalar("SELECT decided_by::text FROM expense_claims WHERE id = :c", c=cid) == winner
    )
    notes = [n for n in await inbox(client, staff, org) if n["type"] == "expense_approved"]
    assert len(notes) == 1


async def test_two_submissions_at_once_give_one_submission(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    claim = await create_claim(client, staff, org, lines=1)
    cid = claim["id"]
    responses = await asyncio.gather(
        client.post(claim_url(org, cid, "/submit"), headers=staff.headers),
        client.post(claim_url(org, cid, "/submit"), headers=staff.headers),
        client.post(claim_url(org, cid, "/submit"), headers=team.owner.headers),
    )
    assert sorted(r.status_code for r in responses) == [200, 409, 409]
    assert await events_of(cid) == ["submitted"]
    assert len(await audit_entries(team.id, "expense_claim.submitted")) == 1
    pending = await sql("SELECT 1 FROM notifications WHERE type = 'expense_pending'")
    assert len(pending) == 1  # the Owner, the only approver who is not the claimant


async def test_an_approval_and_a_recall_at_once_leave_the_claim_in_one_state(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    cid = await submitted_claim(client, team, staff)
    approve, recall = await asyncio.gather(
        client.post(claim_url(org, cid, "/approve"), headers=team.owner.headers),
        client.post(claim_url(org, cid, "/recall"), headers=staff.headers),
    )
    assert sorted([approve.status_code, recall.status_code]) == [200, 409]
    status = await scalar("SELECT status FROM expense_claims WHERE id = :c", c=cid)
    if approve.status_code == 200:
        assert status == "approved" and await events_of(cid) == ["submitted", "approved"]
        assert error(recall)["code"] == "INVALID_STATUS_TRANSITION"
    else:
        assert status == "draft" and await events_of(cid) == ["submitted", "recalled"]
        assert error(approve)["code"] == "INVALID_STATUS_TRANSITION"
        row = await sql("SELECT decided_by, decided_at FROM expense_claims WHERE id = :c", c=cid)
        assert row == [{"decided_by": None, "decided_at": None}]


async def test_an_approval_and_a_return_at_once_give_one_decision(client):
    team = await expense_team(client)
    org = team.org
    staff, admin = await team.member("staff"), await team.member("admin")
    cid = await submitted_claim(client, team, staff)
    approve, back = await asyncio.gather(
        client.post(claim_url(org, cid, "/approve"), headers=team.owner.headers),
        client.post(claim_url(org, cid, "/return"), headers=admin.headers, json={"reason": "No"}),
    )
    assert sorted([approve.status_code, back.status_code]) == [200, 409]
    status = await scalar("SELECT status FROM expense_claims WHERE id = :c", c=cid)
    expected = "approved" if approve.status_code == 200 else "returned"
    assert status == expected
    assert await events_of(cid) == ["submitted", expected]
    decided = await sql("SELECT decided_by, decision_note FROM expense_claims WHERE id = :c", c=cid)
    assert decided[0]["decided_by"] is not None
    assert (decided[0]["decision_note"] == "No") is (expected == "returned")


async def test_overlapping_bulk_approvals_never_deadlock_and_leave_one_winner(client):
    team = await expense_team(client)
    staff, admin = await team.member("staff"), await team.member("admin")
    ids = [await submitted_claim(client, team, staff) for _ in range(3)]
    url = team.url("/expense-approvals/bulk-approve")
    first, second = await asyncio.gather(
        client.post(url, headers=team.owner.headers, json={"claim_ids": [ids[0], ids[1]]}),
        client.post(url, headers=admin.headers, json={"claim_ids": [ids[2], ids[1]]}),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    approved = await sql("SELECT id::text AS id FROM expense_claims WHERE status = 'approved'")
    assert len(approved) == 2 and ids[1] in {row["id"] for row in approved}
    # The refused call changed nothing: its other claim is still waiting.
    waiting = await sql("SELECT id::text AS id FROM expense_claims WHERE status = 'submitted'")
    assert len(waiting) == 1 and waiting[0]["id"] in (ids[0], ids[2])
    assert len(await audit_entries(team.id, "expense_claim.approved")) == 2


async def test_a_line_added_while_the_claim_is_submitted_is_never_lost_or_left_behind(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    body = await line_body(org, description="One more")
    for _ in range(4):  # whichever request gets the claim row first, the outcome is consistent
        claim = await create_claim(client, staff, org, lines=1)
        cid = claim["id"]
        add, submit = await asyncio.gather(
            client.post(claim_url(org, cid, "/lines"), headers=staff.headers, json=body),
            client.post(claim_url(org, cid, "/submit"), headers=staff.headers),
        )
        assert submit.status_code == 200
        lines = await count_rows("SELECT count(*) FROM expenses WHERE claim_id = :c", c=cid)
        # Either the line made it into the submission, or it was refused: never half.
        assert (add.status_code, lines) in {(201, 2), (409, 1)}
        if add.status_code == 409:
            assert error(add)["code"] == "EXPENSE_CLAIM_LOCKED"


async def test_a_receipt_uploaded_while_the_claim_is_submitted_is_never_left_behind(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    claim = await create_claim(client, staff, org, lines=1)
    cid, lid = claim["id"], claim["lines"][0]["id"]
    upload, submit = await asyncio.gather(
        upload_receipt(client, staff, org, lid),
        client.post(claim_url(org, cid, "/submit"), headers=staff.headers),
    )
    assert submit.status_code == 200
    rows = await receipt_rows(lid)
    if upload.status_code == 201:
        assert len(rows) == 1 and len(stored_files()) == 1
    else:
        assert upload.status_code == 409 and error(upload)["code"] == "EXPENSE_CLAIM_LOCKED"
        assert rows == [] and stored_files() == []


async def test_deleting_the_same_receipt_twice_at_once_removes_it_once(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    claim = await create_claim(client, staff, org, lines=1)
    lid = claim["lines"][0]["id"]
    made = ok_data(await upload_receipt(client, staff, org, lid), 201)
    assert len(stored_files()) == 1
    responses = await asyncio.gather(
        *[client.delete(receipt_url(org, lid, made["id"]), headers=staff.headers) for _ in range(3)]
    )
    assert sorted(r.status_code for r in responses) == [200, 404, 404]
    assert await receipt_rows() == [] and stored_files() == []
    assert len(await audit_entries(team.id, "expense_receipt.deleted")) == 1
