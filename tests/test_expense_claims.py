"""Expense claims: create, read, update and delete, who may do what by role, tenancy and the
states in which a claim takes edits."""

from collections import Counter

import pytest

from app.core.permissions import SYSTEM_ROLES
from tests.billing_helpers import count_rows
from tests.clients_helpers import sql
from tests.expense_helpers import (
    add_line,
    claim_url,
    create_claim,
    expense_team,
    line_body,
    make_claim,
    make_line,
)
from tests.factories import add_member, audit_entries
from tests.foundation_helpers import add_custom_role
from tests.time_helpers import err, ok_data, set_manager

CLAIM_KEYS = {
    "id",
    "user_id",
    "user_name",
    "created_by",
    "created_by_name",
    "title",
    "status",
    "submitted_at",
    "decided_at",
    "decided_by",
    "decided_by_name",
    "decision_note",
    "line_count",
    "first_date",
    "last_date",
    "totals",
    "actions",
    "created_at",
    "updated_at",
}
TOTAL_KEYS = {"net", "vat", "gross", "recharge", "reimbursable"}
ACTION_KEYS = {
    "can_edit",
    "can_delete",
    "can_submit",
    "can_recall",
    "can_approve",
    "can_return",
    "can_correct",
}


async def listed(client, team, actor=None, **params) -> list[dict]:
    response = await client.get(
        claim_url(team.org), headers=(actor or team.owner).headers, params={"limit": 100, **params}
    )
    return ok_data(response)


# --- CRUD -----------------------------------------------------------------------------------


async def test_create_read_update_and_delete_a_claim(client):
    team = await expense_team(client)
    org = team.org
    h = team.owner.headers
    data = await create_claim(client, team.owner, org, lines=2, title="Client visit")
    assert set(data) == CLAIM_KEYS | {"lines", "events"}
    assert data["status"] == "draft" and data["title"] == "Client visit"
    assert data["user_id"] == data["created_by"] == str(team.owner.id)
    assert data["user_name"] == data["created_by_name"] == team.owner.full_name
    assert data["submitted_at"] is None and data["decided_at"] is None
    assert data["decided_by"] is None and data["decided_by_name"] is None
    assert data["decision_note"] is None and data["events"] == []
    assert data["line_count"] == 2 and data["first_date"] == data["last_date"] == "2026-09-07"
    assert set(data["totals"]) == TOTAL_KEYS
    assert data["totals"] == {
        "net": 2000,
        "vat": 0,
        "gross": 2000,
        "recharge": 0,
        "reimbursable": 2000,
    }
    assert [line["description"] for line in data["lines"]] == ["Line 1", "Line 2"]
    assert set(data["actions"]) == ACTION_KEYS
    assert data["actions"] == {
        "can_edit": True,
        "can_delete": True,
        "can_submit": True,
        "can_recall": False,
        "can_approve": False,
        "can_return": False,
        "can_correct": False,
    }

    got = await client.get(claim_url(org, data["id"]), headers=h)
    assert ok_data(got) == data

    renamed = await client.patch(claim_url(org, data["id"]), headers=h, json={"title": "Audit"})
    assert ok_data(renamed)["title"] == "Audit"
    # The same title again changes nothing; blank clears it.
    await client.patch(claim_url(org, data["id"]), headers=h, json={"title": "Audit"})
    cleared = await client.patch(claim_url(org, data["id"]), headers=h, json={"title": "  "})
    assert ok_data(cleared)["title"] is None
    assert (await client.patch(claim_url(org, data["id"]), headers=h, json={})).status_code == 200
    for body in ({"title": "t" * 201}, {"status": "approved"}, {"user_id": str(team.owner.id)}):
        patched = await client.patch(claim_url(org, data["id"]), headers=h, json=body)
        assert patched.status_code == 422, body

    deleted = await client.delete(claim_url(org, data["id"]), headers=h)
    assert deleted.status_code == 200 and deleted.json()["data"] is None
    assert (await client.get(claim_url(org, data["id"]), headers=h)).status_code == 404
    assert await count_rows("SELECT count(*) FROM expenses") == 0

    actions = Counter(
        e["action"]
        for e in await audit_entries(team.id)
        if e["target_type"] in ("expense_claim", "expense")
    )
    assert actions == {
        "expense_claim.created": 1,
        "expense.created": 2,
        "expense_claim.updated": 2,
        "expense_claim.deleted": 1,
    }
    [created] = await audit_entries(team.id, "expense_claim.created")
    assert created["metadata"] == {
        "user_id": str(team.owner.id),
        "user_name": team.owner.full_name,
        "title": "Client visit",
        "line_count": 2,
        "gross": 2000,
    }
    updates = await audit_entries(team.id, "expense_claim.updated")
    assert [e["metadata"]["changes"] for e in updates] == [
        {"title": ["Client visit", "Audit"]},
        {"title": ["Audit", None]},
    ]
    [removed] = await audit_entries(team.id, "expense_claim.deleted")
    assert removed["metadata"]["line_count"] == 2 and removed["metadata"]["gross"] == 2000
    rows = await sql(
        "SELECT ip_address::text AS ip, request_id FROM audit_logs "
        "WHERE action LIKE 'expense_claim.%'"
    )
    assert len(rows) == 4 and all(r["ip"] and r["request_id"] for r in rows)


async def test_a_claim_may_have_no_lines_and_a_bad_line_fails_the_whole_call(client):
    team = await expense_team(client)
    org, h = team.org, team.owner.headers
    empty = await create_claim(client, team.owner, org)
    assert empty["title"] is None and empty["lines"] == [] and empty["line_count"] == 0
    assert empty["totals"] == dict.fromkeys(TOTAL_KEYS, 0)
    assert empty["first_date"] is None and empty["last_date"] is None
    before = await count_rows("SELECT count(*) FROM expense_claims")

    good = await line_body(org)
    bad = await line_body(org, net=-5)
    missing_type = {**good, "expense_type_id": "00000000-0000-4000-8000-000000000000"}
    for body, status in (
        ({"lines": [good, bad]}, 422),
        ({"lines": [bad]}, 422),
        ({"lines": [missing_type]}, 404),
        ({"lines": [good] * 101}, 422),
        ({"lines": "nope"}, 422),
        ({"title": "t" * 201}, 422),
        ({"unknown": 1}, 422),
    ):
        response = await client.post(claim_url(org), headers=h, json=body)
        assert response.status_code == status, body
    assert await count_rows("SELECT count(*) FROM expense_claims") == before
    assert await count_rows("SELECT count(*) FROM expenses") == 0
    assert await audit_entries(team.id, "expense.created") == []
    maximum = await client.post(claim_url(org), headers=h, json={"lines": [good] * 100})
    assert maximum.status_code == 201 and maximum.json()["data"]["line_count"] == 100


async def test_claims_are_recorded_on_behalf_of_other_staff(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    manager = await team.member("manager")
    on_behalf = await client.post(
        claim_url(org),
        headers=team.owner.headers,
        json={"user_id": str(staff.id), "title": "On your behalf"},
    )
    data = ok_data(on_behalf, 201)
    assert data["user_id"] == str(staff.id) and data["user_name"] == staff.full_name
    assert data["created_by"] == str(team.owner.id)
    assert data["created_by_name"] == team.owner.full_name
    # The claimant sees it and works on it; the person who entered it still may (expense.manage).
    assert (await client.get(claim_url(org, data["id"]), headers=staff.headers)).status_code == 200
    assert (await add_line(client, staff, org, data["id"]))["claim_id"] == data["id"]
    assert (await add_line(client, team.owner, org, data["id"]))["id"]
    [created] = await audit_entries(team.id, "expense_claim.created")
    assert created["metadata"]["user_id"] == str(staff.id)
    assert created["actor_user_id"] == str(team.owner.id)

    # Without expense.manage nobody records for somebody else.
    refused = await client.post(
        claim_url(org), headers=manager.headers, json={"user_id": str(staff.id)}
    )
    assert err(refused, 403)["code"] == "PERMISSION_DENIED"
    refused = await client.post(
        claim_url(org), headers=staff.headers, json={"user_id": str(manager.id)}
    )
    assert refused.status_code == 403
    # Only for active members of the practice.
    other = await expense_team(client, "Other Practice")
    outsider = await add_member(other.org["id"], "staff")
    unknown = await client.post(
        claim_url(org), headers=team.owner.headers, json={"user_id": str(outsider.id)}
    )
    assert err(unknown, 404)["details"] == {"field": "user_id"}
    gone = await add_member(org["id"], "staff", status="left")
    inactive = await client.post(
        claim_url(org), headers=team.owner.headers, json={"user_id": str(gone.id)}
    )
    assert err(inactive, 422)["details"][0]["field"] == "user_id"


# --- Permissions ----------------------------------------------------------------------------


@pytest.mark.parametrize("role", [r.key for r in SYSTEM_ROLES])
async def test_claim_permissions_by_system_role(client, role):
    team = await expense_team(client)
    org = team.org
    actor = await team.member(role)
    grants = next(r.permissions for r in SYSTEM_ROLES if r.key == role)
    any_permission = bool(
        grants & {"expense.log", "expense.read_all", "expense.manage", "expense.approve"}
    )
    can_log = bool(grants & {"expense.log", "expense.manage"})
    sees_all = bool(grants & {"expense.read_all", "expense.manage"})
    can_manage = "expense.manage" in grants

    other = await add_member(org["id"], "staff")
    theirs = await make_claim(org, other, title="Theirs")
    await make_line(org, theirs, description="Their taxi")
    mine = await make_claim(org, actor, title="Mine")

    listing = await client.get(claim_url(org), headers=actor.headers)
    summary = await client.get(claim_url(org, tail="/summary"), headers=actor.headers)
    lines = await client.get(team.url("/expenses"), headers=actor.headers)
    expected = 200 if any_permission else 403
    assert [r.status_code for r in (listing, summary, lines)] == [expected] * 3
    if any_permission:
        seen = {c["id"] for c in listing.json()["data"]}
        assert (str(theirs) in seen) is sees_all and str(mine) in seen
        assert ({line["claim_id"] for line in lines.json()["data"]} == {str(theirs)}) is sees_all

    created = await client.post(claim_url(org), headers=actor.headers, json={"title": "New"})
    assert created.status_code == (201 if can_log else 403)
    got_theirs = await client.get(claim_url(org, theirs), headers=actor.headers)
    assert got_theirs.status_code == (200 if sees_all else 403)
    edited = await client.patch(claim_url(org, theirs), headers=actor.headers, json={"title": "X"})
    added = await client.post(
        claim_url(org, theirs, "/lines"), headers=actor.headers, json=await line_body(org)
    )
    assert [edited.status_code, added.status_code] == ([200, 201] if can_manage else [403, 403])
    removed = await client.delete(claim_url(org, theirs), headers=actor.headers)
    assert removed.status_code == (200 if can_manage else 403)
    # One's own claims are written with expense.log (or expense.manage).
    own_edit = await client.patch(claim_url(org, mine), headers=actor.headers, json={"title": "Y"})
    assert own_edit.status_code == (200 if can_log else 403)


async def test_an_approver_sees_the_claims_of_the_people_they_manage(client):
    team = await expense_team(client)
    org = team.org
    await add_custom_role(
        org["id"],
        "Approver",
        ["organization.read", "member.read", "expense.log", "expense.approve"],
    )
    approver = await add_member(org["id"], "Approver", full_name="Ann Approver")
    report = await add_member(org["id"], "staff", full_name="Rita Report")
    stranger = await add_member(org["id"], "staff", full_name="Sam Stranger")
    await set_manager(org, report, approver)
    mine = await make_claim(org, approver, title="Ann's own")
    theirs = await make_claim(org, report, status="submitted", title="Rita's")
    foreign = await make_claim(org, stranger, status="submitted", title="Sam's")
    await make_line(org, theirs, net=1500)

    assert {c["id"] for c in await listed(client, team, approver)} == {str(mine), str(theirs)}
    # Her report's claim is hers to read, but not to write; the stranger's is closed to her.
    assert (await client.get(claim_url(org, theirs), headers=approver.headers)).status_code == 200
    assert (await client.get(claim_url(org, foreign), headers=approver.headers)).status_code == 403
    edit = await client.patch(claim_url(org, theirs), headers=approver.headers, json={"title": "x"})
    assert edit.status_code == 403
    detail = ok_data(await client.get(claim_url(org, theirs), headers=approver.headers))
    assert detail["actions"]["can_approve"] is True and detail["actions"]["can_edit"] is False
    assert len(detail["lines"]) == 1
    # The stranger's own list holds only the stranger's claim.
    assert {c["id"] for c in await listed(client, team, stranger)} == {str(foreign)}
    assert await listed(client, team, approver, user_id=str(stranger.id)) == []


# --- Tenancy and locked states --------------------------------------------------------------


async def test_another_practices_claims_are_not_found(client):
    team = await expense_team(client)
    other = await expense_team(client, "Other Practice")
    org = team.org
    theirs = await create_claim(client, other.owner, other.org, lines=1)
    theirs_id, line_id = theirs["id"], theirs["lines"][0]["id"]
    h = team.owner.headers
    attempts = [
        await client.get(claim_url(org, theirs_id), headers=h),
        await client.patch(claim_url(org, theirs_id), headers=h, json={"title": "x"}),
        await client.delete(claim_url(org, theirs_id), headers=h),
        await client.post(
            claim_url(org, theirs_id, "/lines"), headers=h, json=await line_body(org)
        ),
        await client.patch(
            claim_url(org, theirs_id, f"/lines/{line_id}"), headers=h, json={"net": 5}
        ),
        await client.delete(claim_url(org, theirs_id, f"/lines/{line_id}"), headers=h),
        await client.post(claim_url(org, theirs_id, "/submit"), headers=h),
        await client.post(claim_url(org, theirs_id, "/recall"), headers=h),
        await client.post(claim_url(org, theirs_id, "/approve"), headers=h),
        await client.post(claim_url(org, theirs_id, "/return"), headers=h, json={"reason": "x"}),
        await client.post(
            team.url("/expense-approvals/bulk-approve"), headers=h, json={"claim_ids": [theirs_id]}
        ),
        await client.post(
            team.url(f"/expenses/{line_id}/receipts"), headers=h, files={"file": ("a.png", b"x")}
        ),
    ]
    for response in attempts:
        assert err(response, 404)["code"] == "RESOURCE_NOT_FOUND"
    # A line of one's own claim is not reachable through another claim either.
    mine = await create_claim(client, team.owner, org, lines=1)
    wrong = await client.patch(
        claim_url(org, theirs_id, f"/lines/{mine['lines'][0]['id']}"), headers=h, json={"net": 5}
    )
    assert wrong.status_code == 404
    assert theirs_id not in {c["id"] for c in await listed(client, team)}
    # The other practice's URLs are closed to this practice's owner.
    assert (await client.get(claim_url(other.org), headers=h)).status_code == 404
    assert await count_rows("SELECT count(*) FROM expense_claims") == 2
    assert (await client.get(claim_url(org, "not-a-uuid"), headers=h)).status_code == 422


async def test_only_draft_and_returned_claims_take_edits(client):
    team = await expense_team(client)
    org, h = team.org, team.owner.headers
    states = {}
    for status in ("draft", "submitted", "approved", "returned"):
        claim = await make_claim(org, team.owner, status=status, decided_by=team.owner)
        states[status] = (claim, await make_line(org, claim))
    for status, (claim, line) in states.items():
        editable = status in ("draft", "returned")
        title = await client.patch(claim_url(org, claim), headers=h, json={"title": "New"})
        added = await client.post(
            claim_url(org, claim, "/lines"), headers=h, json=await line_body(org)
        )
        edited = await client.patch(
            claim_url(org, claim, f"/lines/{line}"), headers=h, json={"description": "Changed"}
        )
        removed = await client.delete(claim_url(org, claim, f"/lines/{line}"), headers=h)
        # An approved claim's line can be corrected, but only with a reason (docs: 422 here).
        expected = [200, 201, 200, 200] if editable else [409, 409, 409, 409]
        if status == "approved":
            expected[2] = 422
        assert [r.status_code for r in (title, added, edited, removed)] == expected, status
        if not editable:
            for response in (title, added, removed):
                assert response.json()["error"]["code"] == "EXPENSE_CLAIM_LOCKED"
    reasonless = await client.patch(
        claim_url(org, states["approved"][0], f"/lines/{states['approved'][1]}"),
        headers=h,
        json={"net": 5},
    )
    assert err(reasonless, 422)["code"] == "EXPENSE_CORRECTION_REASON_REQUIRED"
    # Only a draft is deleted.
    for status in ("submitted", "approved", "returned"):
        refused = await client.delete(claim_url(org, states[status][0]), headers=h)
        assert err(refused, 409)["code"] == "EXPENSE_CLAIM_LOCKED"
    assert (await client.delete(claim_url(org, states["draft"][0]), headers=h)).status_code == 200
