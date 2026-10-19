"""The expense claim workflow: submit, recall, approve, return, bulk approve and the approval queue;
the receipt rule, who may decide, self-approval and the history of a claim."""

import uuid
from datetime import UTC, datetime

import pytest

from tests.billing_helpers import count_rows, set_org
from tests.clients_helpers import scalar, sql
from tests.expense_helpers import (
    act,
    add_line,
    claim_url,
    create_claim,
    expense_team,
    make_claim,
    make_line,
    upload_receipt,
)
from tests.factories import add_member, audit_entries, error
from tests.foundation_helpers import add_custom_role
from tests.ticket_helpers import SAMPLES
from tests.time_helpers import err, ok_data, set_manager


async def submitted(client, actor, org, lines: int = 1, **extra) -> dict:
    """A claim of ``actor`` with ``lines`` lines of 10.00, submitted."""
    claim = await create_claim(client, actor, org, lines=lines, **extra)
    return await act(client, actor, org, claim["id"], "submit")


async def approver_role(org: dict) -> None:
    await add_custom_role(
        org["id"],
        "Approver",
        ["organization.read", "member.read", "expense.log", "expense.approve"],
    )


async def status_of(claim_id: str | uuid.UUID) -> str:
    return await scalar("SELECT status FROM expense_claims WHERE id = :i", i=claim_id)


# --- The cycle ------------------------------------------------------------------------------


async def test_a_claim_goes_through_submission_return_resubmission_and_approval(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    manager = await team.member("manager")
    await set_manager(org, staff, manager)
    claim = await create_claim(client, staff, org, lines=2, title="Leeds trip")
    cid = claim["id"]

    first = await act(client, staff, org, cid, "submit")
    assert first["status"] == "submitted" and first["submitted_at"]
    assert first["decided_at"] is None and first["decided_by"] is None
    assert [e["action"] for e in first["events"]] == ["submitted"]
    assert first["events"][0]["actor_user_id"] == str(staff.id)
    assert (
        first["events"][0]["actor_name"] == staff.full_name and first["events"][0]["note"] is None
    )
    assert first["actions"] == {
        "can_edit": False,
        "can_delete": False,
        "can_submit": False,
        "can_recall": True,
        "can_approve": False,
        "can_return": False,
        "can_correct": False,
    }
    # Locked while it waits.
    locked = await client.patch(claim_url(org, cid), headers=staff.headers, json={"title": "x"})
    assert err(locked, 409)["code"] == "EXPENSE_CLAIM_LOCKED"

    # The manager sees what she can do and returns it with a reason.
    seen = ok_data(await client.get(claim_url(org, cid), headers=manager.headers))
    assert seen["actions"]["can_approve"] is True and seen["actions"]["can_return"] is True
    returned = await act(
        client, manager, org, cid, "return", json={"reason": "Please add the dates"}
    )
    assert returned["status"] == "returned" and returned["decision_note"] == "Please add the dates"
    assert returned["decided_by"] == str(manager.id)
    assert returned["decided_by_name"] == manager.full_name and returned["decided_at"]
    mine = ok_data(await client.get(claim_url(org, cid), headers=staff.headers))
    assert mine["actions"]["can_edit"] is True and mine["actions"]["can_submit"] is True
    assert mine["actions"]["can_approve"] is False and mine["actions"]["can_delete"] is False

    # The claimant edits it and submits it again: the decision is cleared, the history stays.
    await add_line(client, staff, org, cid)
    again = await act(client, staff, org, cid, "submit")
    assert again["status"] == "submitted" and again["decision_note"] is None
    assert again["decided_by"] is None and again["decided_at"] is None
    assert [e["action"] for e in again["events"]] == ["submitted", "returned", "submitted"]
    assert again["events"][1]["note"] == "Please add the dates"
    assert again["events"][1]["actor_user_id"] == str(manager.id)

    approved = await act(client, manager, org, cid, "approve", json={"note": "Thanks"})
    assert approved["status"] == "approved" and approved["decision_note"] == "Thanks"
    assert approved["decided_by"] == str(manager.id) and approved["decided_at"]
    assert [e["action"] for e in approved["events"]] == [
        "submitted",
        "returned",
        "submitted",
        "approved",
    ]
    assert approved["events"][3]["note"] == "Thanks"
    assert approved["line_count"] == 3 and approved["totals"]["gross"] == 3000
    # Approved claims are immutable for the claimant.
    assert approved["actions"]["can_edit"] is False and approved["actions"]["can_recall"] is False
    edit = await client.patch(claim_url(org, cid), headers=staff.headers, json={"title": "x"})
    assert err(edit, 409)["code"] == "EXPENSE_CLAIM_LOCKED"
    detail = ok_data(await client.get(claim_url(org, cid), headers=staff.headers))
    assert [e["created_at"] for e in detail["events"]] == sorted(
        e["created_at"] for e in detail["events"]
    )

    entries = [e for e in await audit_entries(org["id"]) if e["target_type"] == "expense_claim"]
    submitted_rows = [e["metadata"] for e in entries if e["action"] == "expense_claim.submitted"]
    assert [(m["old_status"], m["new_status"]) for m in submitted_rows] == [
        ("draft", "submitted"),
        ("returned", "submitted"),
    ]
    [ret] = await audit_entries(org["id"], "expense_claim.returned")
    assert (
        ret["actor_user_id"] == str(manager.id)
        and ret["metadata"]["note"] == "Please add the dates"
    )
    assert ret["metadata"]["user_id"] == str(staff.id) and ret["metadata"]["gross"] == 2000
    [appr] = await audit_entries(org["id"], "expense_claim.approved")
    assert appr["metadata"]["old_status"] == "submitted" and appr["metadata"]["gross"] == 3000
    rows = await sql(
        "SELECT ip_address::text AS ip, request_id FROM audit_logs "
        "WHERE action IN ('expense_claim.submitted', 'expense_claim.returned', 'expense_claim.approved')"
    )
    assert len(rows) == 4 and all(r["ip"] and r["request_id"] for r in rows)


async def test_a_recalled_claim_is_a_draft_again(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    cid = (await submitted(client, staff, org))["id"]
    recalled = await act(client, staff, org, cid, "recall")
    assert recalled["status"] == "draft" and recalled["submitted_at"] is None
    assert [e["action"] for e in recalled["events"]] == ["submitted", "recalled"]
    assert recalled["actions"]["can_edit"] is True and recalled["actions"]["can_delete"] is True
    assert (await add_line(client, staff, org, cid))["claim_id"] == cid
    resubmitted = await act(client, staff, org, cid, "submit")
    assert [e["action"] for e in resubmitted["events"]] == ["submitted", "recalled", "submitted"]
    assert len(await audit_entries(org["id"], "expense_claim.recalled")) == 1


async def test_every_transition_needs_the_right_status(client):
    team = await expense_team(client)
    org, owner = team.org, team.owner
    claims = {}
    for status in ("draft", "submitted", "approved", "returned"):
        claims[status] = await make_claim(org, owner, status=status, decided_by=owner)
        await make_line(org, claims[status])
    allowed = {
        "submit": {"draft", "returned"},
        "recall": {"submitted"},
        "approve": {"submitted"},
        "return": {"submitted"},
    }
    for action, statuses in allowed.items():
        for status, claim in claims.items():
            if status in statuses:
                continue  # these would succeed (and change the claim): the cycle tests cover them
            body = {"reason": "No"} if action == "return" else None
            response = await client.post(
                claim_url(org, claim, f"/{action}"), headers=owner.headers, json=body
            )
            failure = err(response, 409)
            assert failure["code"] == "INVALID_STATUS_TRANSITION", (action, status)
            assert failure["details"]["from"] == status
    # The refusals changed nothing.
    for status, claim in claims.items():
        assert await status_of(claim) == status
    assert await count_rows("SELECT count(*) FROM expense_claim_events") == 0


async def test_submit_needs_a_line_and_only_the_claimant_or_a_manager_may_submit_or_recall(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    other = await add_member(org["id"], "staff")
    empty = await create_claim(client, staff, org)
    refused = await client.post(claim_url(org, empty["id"], "/submit"), headers=staff.headers)
    body = err(refused, 422)
    assert body["code"] == "EXPENSE_CLAIM_EMPTY" and body["details"][0]["field"] == "lines"
    assert await status_of(empty["id"]) == "draft"

    claim = await create_claim(client, staff, org, lines=1)
    for actor in (other, await team.member("manager"), await team.member("billing")):
        response = await client.post(claim_url(org, claim["id"], "/submit"), headers=actor.headers)
        assert response.status_code == 403
    assert await status_of(claim["id"]) == "draft"
    # expense.manage submits on behalf (staff without a login depend on it) and recalls.
    done = await act(client, team.owner, org, claim["id"], "submit")
    assert done["status"] == "submitted" and done["events"][0]["actor_user_id"] == str(
        team.owner.id
    )
    for actor in (other, await team.member("manager")):
        response = await client.post(claim_url(org, claim["id"], "/recall"), headers=actor.headers)
        assert response.status_code == 403
    back = await act(client, team.owner, org, claim["id"], "recall")
    assert back["status"] == "draft"


# --- The receipt rule -----------------------------------------------------------------------


async def test_lines_above_the_threshold_need_a_receipt_to_submit(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    claim = await create_claim(client, staff, org)
    cid = claim["id"]
    # The threshold is 25.00 (2,500 pence, gross): at it, no receipt; one penny more, a receipt.
    at_limit = await add_line(client, staff, org, cid, net=2500, vat=0)
    below = await add_line(client, staff, org, cid, net=2499, vat=0)
    above = await add_line(client, staff, org, cid, net=2501, vat=0)
    paid_by_practice = await add_line(client, staff, org, cid, net=9000, vat=0, payer="practice")
    vatted = await add_line(client, staff, org, cid, "OTHER", net=2200)  # 26.40 with VAT
    mileage = await add_line(client, staff, org, cid, "MILEAGE", net=None, miles=500)  # 225.00
    assert [
        line["receipt_required"]
        for line in (at_limit, below, above, paid_by_practice, vatted, mileage)
    ] == [
        False,
        False,
        True,
        True,
        True,
        False,
    ]
    refused = await client.post(claim_url(org, cid, "/submit"), headers=staff.headers)
    failure = err(refused, 422)
    assert failure["code"] == "RECEIPT_REQUIRED"
    assert {d["line_id"] for d in failure["details"]} == {
        above["id"],
        paid_by_practice["id"],
        vatted["id"],
    }
    assert {d["field"] for d in failure["details"]} == {"lines"}
    assert {d["code"] for d in failure["details"]} == {"receipt_required"}
    assert (
        await status_of(cid) == "draft"
        and await count_rows("SELECT count(*) FROM expense_claim_events") == 0
    )

    for line in (above, paid_by_practice):
        assert (await upload_receipt(client, staff, org, line["id"])).status_code == 201
    still = err(await client.post(claim_url(org, cid, "/submit"), headers=staff.headers), 422)
    assert [d["line_id"] for d in still["details"]] == [vatted["id"]]
    pdf = SAMPLES["pdf"]
    done = await upload_receipt(
        client, staff, org, vatted["id"], pdf.name, pdf.data, pdf.content_type
    )
    assert done.status_code == 201
    final = await act(client, staff, org, cid, "submit")
    assert final["status"] == "submitted" and final["line_count"] == 6
    assert [line["receipt_count"] for line in final["lines"]].count(1) == 3


async def test_the_threshold_is_the_practice_setting(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    claim = await create_claim(client, staff, org)
    small = await add_line(client, staff, org, claim["id"], net=100, vat=0)
    await set_org(org, receipt_required_threshold=100)
    assert (await act(client, staff, org, claim["id"], "submit"))["status"] == "submitted"
    await act(client, staff, org, claim["id"], "recall")
    await set_org(org, receipt_required_threshold=99)
    failure = err(
        await client.post(claim_url(org, claim["id"], "/submit"), headers=staff.headers), 422
    )
    assert [d["line_id"] for d in failure["details"]] == [small["id"]]
    await set_org(org, receipt_required_threshold=0)
    assert err(
        await client.post(claim_url(org, claim["id"], "/submit"), headers=staff.headers), 422
    )
    detail = ok_data(await client.get(claim_url(org, claim["id"]), headers=staff.headers))
    assert detail["lines"][0]["receipt_required"] is True
    await set_org(org, receipt_required_threshold=10**9)
    assert (await act(client, staff, org, claim["id"], "submit"))["status"] == "submitted"


# --- Who may decide -------------------------------------------------------------------------


async def test_who_may_approve_and_return(client):
    team = await expense_team(client)
    org = team.org
    await approver_role(org)
    staff = await team.member("staff")
    boss = await add_member(org["id"], "Approver", full_name="Bea Boss")  # manages staff
    other_boss = await add_member(org["id"], "Approver", full_name="Otto Other")
    manager = await team.member("manager")  # expense.read_all: any claim
    billing = await team.member("billing")  # read_all, no expense.approve
    await set_manager(org, staff, boss)

    cid = (await submitted(client, staff, org))["id"]
    for actor in (staff, billing, await team.member("viewer")):
        for action, body in (("approve", None), ("return", {"reason": "x"})):
            response = await client.post(
                claim_url(org, cid, f"/{action}"), headers=actor.headers, json=body
            )
            assert err(response, 403)["code"] == "PERMISSION_DENIED"
    # An approver who neither manages the claimant nor reads everything is refused.
    for action, body in (("approve", None), ("return", {"reason": "x"})):
        response = await client.post(
            claim_url(org, cid, f"/{action}"), headers=other_boss.headers, json=body
        )
        assert err(response, 403)["code"] == "PERMISSION_DENIED"
    assert await status_of(cid) == "submitted"
    # The claimant's manager may; so may anyone with expense.read_all and expense.approve.
    assert (await act(client, boss, org, cid, "approve"))["status"] == "approved"
    second = (await submitted(client, staff, org))["id"]
    assert (await act(client, manager, org, second, "return", json={"reason": "No"}))["status"] == (
        "returned"
    )
    third = (await submitted(client, staff, org))["id"]
    assert (await act(client, team.owner, org, third, "approve"))["status"] == "approved"


async def test_nobody_decides_on_their_own_claim_without_organization_update(client):
    team = await expense_team(client)
    org = team.org
    manager = await team.member("manager")  # approves and reads all, cannot update the practice
    admin = await team.member("admin")  # organization.update
    own = (await submitted(client, manager, org))["id"]
    for action, body in (("approve", None), ("return", {"reason": "x"})):
        response = await client.post(
            claim_url(org, own, f"/{action}"), headers=manager.headers, json=body
        )
        assert err(response, 403)["code"] == "SELF_APPROVAL_FORBIDDEN"
    assert await status_of(own) == "submitted"
    detail = ok_data(await client.get(claim_url(org, own), headers=manager.headers))
    assert detail["actions"]["can_approve"] is False and detail["actions"]["can_return"] is False
    # Someone else decides on it; and those who may update the practice decide on their own.
    assert (await act(client, admin, org, own, "approve"))["status"] == "approved"
    mine = (await submitted(client, admin, org))["id"]
    assert (await act(client, admin, org, mine, "approve"))["status"] == "approved"
    owners = (await submitted(client, team.owner, org))["id"]
    assert (await act(client, team.owner, org, owners, "return", json={"reason": "Redo"}))[
        "status"
    ] == "returned"


async def test_returning_needs_a_reason(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    cid = (await submitted(client, staff, org))["id"]
    for body in (None, {}, {"reason": ""}, {"reason": "   "}, {"reason": None}):
        response = await client.post(
            claim_url(org, cid, "/return"), headers=team.owner.headers, json=body
        )
        failure = err(response, 422)
        assert failure["code"] == "EXPENSE_RETURN_REASON_REQUIRED", body
        assert failure["details"][0]["field"] == "reason"
    too_long = await client.post(
        claim_url(org, cid, "/return"), headers=team.owner.headers, json={"reason": "r" * 2001}
    )
    assert too_long.status_code == 422
    unknown = await client.post(
        claim_url(org, cid, "/return"), headers=team.owner.headers, json={"why": "no"}
    )
    assert unknown.status_code == 422
    assert await status_of(cid) == "submitted"
    assert await count_rows("SELECT count(*) FROM expense_claim_events") == 1  # only the submission
    done = await act(client, team.owner, org, cid, "return", json={"reason": "  Add receipts  "})
    assert done["decision_note"] == "Add receipts"
    # Approving takes an optional note and refuses an unknown field.
    again = await act(client, staff, org, cid, "submit")
    assert again["decision_note"] is None
    bad = await client.post(
        claim_url(org, cid, "/approve"), headers=team.owner.headers, json={"x": 1}
    )
    assert bad.status_code == 422
    assert (await act(client, team.owner, org, cid, "approve"))["decision_note"] is None


# --- Bulk approval --------------------------------------------------------------------------


async def test_bulk_approval_is_all_or_nothing(client):
    team = await expense_team(client)
    org, owner = team.org, team.owner
    staff = await team.member("staff")
    ids = [(await submitted(client, staff, org))["id"] for _ in range(3)]
    url = team.url("/expense-approvals/bulk-approve")
    draft = (await create_claim(client, staff, org, lines=1))["id"]

    # One claim that is not submitted refuses all of them.
    response = await client.post(url, headers=owner.headers, json={"claim_ids": [*ids, draft]})
    assert err(response, 409)["code"] == "INVALID_STATUS_TRANSITION"
    # So does one that does not exist (404 names it) ...
    ghost = str(uuid.uuid4())
    response = await client.post(url, headers=owner.headers, json={"claim_ids": [*ids, ghost]})
    assert err(response, 404)["details"] == {"claim_ids": [ghost]}
    # ... and one the caller may not decide on (their own, without organization.update).
    manager = await team.member("manager")
    own = (await submitted(client, manager, org))["id"]
    response = await client.post(url, headers=manager.headers, json={"claim_ids": [*ids, own]})
    assert err(response, 403)["code"] == "SELF_APPROVAL_FORBIDDEN"
    for claim_id in ids:
        assert await status_of(claim_id) == "submitted"
    assert await audit_entries(org["id"], "expense_claim.approved") == []

    ok = await client.post(url, headers=owner.headers, json={"claim_ids": [*ids, ids[0]]})
    data = ok_data(ok)
    assert [c["id"] for c in data] == ids and ok.json()["message"] == "3 expense claims approved"
    assert all(c["status"] == "approved" and c["decided_by"] == str(owner.id) for c in data)
    assert [c["totals"]["gross"] for c in data] == [1000, 1000, 1000]
    assert len(await audit_entries(org["id"], "expense_claim.approved")) == 3
    again = await client.post(url, headers=owner.headers, json={"claim_ids": ids})
    assert err(again, 409)["code"] == "INVALID_STATUS_TRANSITION"

    for body in (
        {"claim_ids": []},
        {"claim_ids": ["x"]},
        {},
        {"claim_ids": [ids[0]], "extra": 1},
    ):
        assert (await client.post(url, headers=owner.headers, json=body)).status_code == 422
    many = [str(uuid.uuid4()) for _ in range(101)]
    over = await client.post(url, headers=owner.headers, json={"claim_ids": many})
    assert over.status_code == 422
    for actor in (staff, await team.member("billing"), await team.member("viewer")):
        refused = await client.post(url, headers=actor.headers, json={"claim_ids": [ids[0]]})
        assert error(refused)["code"] == "PERMISSION_DENIED"


async def test_an_approver_without_read_all_bulk_approves_their_reports_only(client):
    team = await expense_team(client)
    org = team.org
    await approver_role(org)
    boss = await add_member(org["id"], "Approver")
    report = await add_member(org["id"], "staff")
    stranger = await add_member(org["id"], "staff")
    await set_manager(org, report, boss)
    reports_claim = (await submitted(client, report, org))["id"]
    strangers_claim = (await submitted(client, stranger, org))["id"]
    url = team.url("/expense-approvals/bulk-approve")
    both = {"claim_ids": [reports_claim, strangers_claim]}
    refused = await client.post(url, headers=boss.headers, json=both)
    assert err(refused, 403)["code"] == "PERMISSION_DENIED"
    assert await status_of(reports_claim) == "submitted"
    done = await client.post(url, headers=boss.headers, json={"claim_ids": [reports_claim]})
    assert ok_data(done)[0]["status"] == "approved"


# --- The queue ------------------------------------------------------------------------------


async def test_the_approval_queue(client):
    team = await expense_team(client)
    org = team.org
    await approver_role(org)
    boss = await add_member(org["id"], "Approver", full_name="Bea Boss")
    anna = await add_member(org["id"], "staff", full_name="Anna Ames")
    ben = await add_member(org["id"], "staff", full_name="Ben Bell")
    await set_manager(org, anna, boss)
    # Submitted oldest first, whatever the order they were created in.
    first = await make_claim(org, ben, status="submitted", title="Ben's trip")
    await make_line(org, first, net=700, description="Taxi to Leeds")
    older = await make_claim(
        org,
        anna,
        status="submitted",
        title="Anna's trip",
        submitted_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    await make_line(org, older, net=300)
    await make_claim(org, anna, status="draft")
    await make_claim(org, anna, status="approved", decided_by=team.owner)
    manager = await team.member("manager")
    own = await make_claim(
        org,
        manager,
        status="submitted",
        title="Manager's own",
        submitted_at=datetime(2026, 9, 12, tzinfo=UTC),
    )

    url = team.url("/expense-approvals")

    async def queue(actor, **params):
        response = await client.get(url, headers=actor.headers, params={"limit": 100, **params})
        return [c["id"] for c in ok_data(response)]

    # The Owner reads everything and may approve their own: all submitted claims, oldest first.
    assert await queue(team.owner) == [str(older), str(first), str(own)]
    # A manager who may not approve their own claim does not see it.
    assert await queue(manager) == [str(older), str(first)]
    # An approver without expense.read_all sees the claims of the people they manage.
    assert await queue(boss) == [str(older)]
    assert await queue(team.owner, user_id=str(ben.id)) == [str(first)]
    assert await queue(team.owner, q="leeds") == [str(first)]
    assert await queue(team.owner, q="ANNA") == [str(older)]
    assert await queue(team.owner, user_id=str(anna.id), q="ben") == []
    data = ok_data(await client.get(url, headers=boss.headers))
    assert data[0]["user_name"] == "Anna Ames" and data[0]["totals"]["gross"] == 300
    assert data[0]["actions"]["can_approve"] is True
    # Pages of one walk the queue once.
    seen, cursor = [], None
    for _ in range(5):
        params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
        response = await client.get(url, headers=team.owner.headers, params=params)
        seen += [c["id"] for c in ok_data(response)]
        cursor = response.json()["meta"]["next_cursor"]
        if not cursor:
            break
    assert seen == [str(older), str(first), str(own)]
    # An approver with nobody to approve sees an empty queue; others have none at all.
    lonely = await add_member(org["id"], "Approver")
    assert await queue(lonely) == []
    for actor in (
        await team.member("staff"),
        await team.member("billing"),
        await team.member("viewer"),
    ):
        refused = await client.get(url, headers=actor.headers)
        assert err(refused, 403)["code"] == "PERMISSION_DENIED"
    # An approved claim leaves the queue.
    await act(client, team.owner, org, first, "approve")
    assert await queue(team.owner) == [str(older), str(own)]


@pytest.mark.parametrize("bad", [{"limit": 0}, {"limit": 101}, {"cursor": "x"}, {"user_id": "x"}])
async def test_the_queue_refuses_bad_parameters(client, bad):
    team = await expense_team(client)
    response = await client.get(
        team.url("/expense-approvals"), headers=team.owner.headers, params=bad
    )
    assert response.status_code == 422
