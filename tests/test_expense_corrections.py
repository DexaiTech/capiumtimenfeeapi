"""Corrections of approved claims: only someone with ``expense.manage`` may change an approved
claim's lines, with a reason; the claim keeps its status, the change is audited and the claim's
history says so."""

from tests.billing_helpers import count_rows, set_org
from tests.clients_helpers import sql
from tests.domain_factories import make_client
from tests.expense_helpers import (
    act,
    add_line,
    claim_url,
    create_claim,
    expense_team,
    line_row,
    patch_line,
)
from tests.factories import add_member, audit_entries
from tests.foundation_helpers import add_custom_role
from tests.time_helpers import err, ok_data


async def approved_claim(client, team, **line):
    """A billable 10.00 line of a Staff member's claim, approved by the Owner."""
    org = team.org
    staff = await team.member("staff")
    acme = await make_client(org, "Acme Ltd")
    claim = await create_claim(client, staff, org)
    added = await add_line(
        client, staff, org, claim["id"], client_id=str(acme.id), is_billable=True, **line
    )
    await act(client, staff, org, claim["id"], "submit")
    await act(client, team.owner, org, claim["id"], "approve")
    return staff, claim["id"], added


async def test_an_approved_line_is_corrected_with_a_reason_and_the_claim_says_so(client):
    team = await expense_team(client)
    org, owner = team.org, team.owner
    await set_org(org, expense_markup_bp=1_000)
    staff, cid, line = await approved_claim(client, team)
    lid = line["id"]
    assert (line["net"], line["gross"], line["recharge_amount"]) == (1000, 1000, 1100)

    fixed = await patch_line(
        client, owner, org, cid, lid, net=2000, reason="The receipt shows 20.00"
    )
    assert (fixed["net"], fixed["vat"], fixed["gross"], fixed["recharge_amount"]) == (
        2000,
        0,
        2000,
        2200,
    )
    detail = ok_data(await client.get(claim_url(org, cid), headers=owner.headers))
    assert detail["status"] == "approved" and detail["decided_by"] == str(owner.id)
    assert detail["totals"]["gross"] == 2000 and detail["totals"]["recharge"] == 2200
    assert [e["action"] for e in detail["events"]] == ["submitted", "approved", "corrected"]
    corrected = detail["events"][-1]
    assert corrected["note"] == "The receipt shows 20.00"
    assert corrected["actor_user_id"] == str(owner.id) and corrected["actor_name"]
    assert detail["lines"][0]["net"] == 2000
    # The claimant sees the same history and the corrected amounts.
    mine = ok_data(await client.get(claim_url(org, cid), headers=staff.headers))
    assert mine["events"][-1]["action"] == "corrected" and mine["totals"]["gross"] == 2000

    [entry] = await audit_entries(org["id"], "expense.corrected")
    assert entry["actor_user_id"] == str(owner.id) and entry["target_id"] == lid
    assert entry["metadata"]["changes"] == {
        "net": [1000, 2000],
        "gross": [1000, 2000],
        "recharge_amount": [1100, 2200],
    }
    assert entry["metadata"]["reason"] == "The receipt shows 20.00"
    assert entry["metadata"]["claim_id"] == cid and entry["metadata"]["claimant_id"] == str(
        staff.id
    )
    assert await audit_entries(org["id"], "expense.updated") == []
    [row] = await sql(
        "SELECT ip_address::text AS ip, request_id FROM audit_logs "
        "WHERE action = 'expense.corrected'"
    )
    assert row["ip"] and row["request_id"]

    # More than one field at a time, the override of the recharge included.
    again = await patch_line(
        client,
        owner,
        org,
        cid,
        lid,
        description="Client lunch",
        supplier="Cafe Rouge",
        recharge_amount=1500,
        reason="Wrong description",
    )
    assert again["description"] == "Client lunch" and again["recharge_amount"] == 1500
    assert [e["action"] for e in (await _events(client, owner, org, cid))] == [
        "submitted",
        "approved",
        "corrected",
        "corrected",
    ]


async def _events(client, actor, org, cid) -> list[dict]:
    return ok_data(await client.get(claim_url(org, cid), headers=actor.headers))["events"]


async def test_a_correction_needs_a_reason_and_a_change_without_one_is_a_no_op(client):
    team = await expense_team(client)
    org, owner = team.org, team.owner
    _, cid, line = await approved_claim(client, team)
    lid = line["id"]
    for body in ({}, {"reason": ""}, {"reason": "   "}, {"reason": None}):
        failure = await patch_line(client, owner, org, cid, lid, status=422, net=3000, **body)
        assert failure["code"] == "EXPENSE_CORRECTION_REASON_REQUIRED", body
        assert failure["details"][0]["field"] == "reason"
    too_long = await patch_line(
        client, owner, org, cid, lid, status=422, net=3000, reason="r" * 501
    )
    assert too_long["details"][0]["field"] == "reason"
    assert (await line_row(lid))["net"] == 1000
    assert await audit_entries(org["id"], "expense.corrected") == []

    # A reason with nothing to change writes nothing: no audit row, no history entry.
    same = await patch_line(client, owner, org, cid, lid, net=1000, reason="Checked, all fine")
    assert same["net"] == 1000
    assert await audit_entries(org["id"], "expense.corrected") == []
    assert [e["action"] for e in await _events(client, owner, org, cid)] == [
        "submitted",
        "approved",
    ]
    # A reason on a draft is harmless and is kept in the audit row, not in the claim's history.
    draft = await create_claim(client, owner, org)
    mine = await add_line(client, owner, org, draft["id"])
    await patch_line(client, owner, org, draft["id"], mine["id"], net=1200, reason="Typo")
    [entry] = await audit_entries(org["id"], "expense.updated")
    assert entry["metadata"]["reason"] == "Typo"
    assert await _events(client, owner, org, draft["id"]) == []


async def test_a_billed_line_cannot_be_corrected(client):
    team = await expense_team(client)
    org, owner = team.org, team.owner
    _, cid, line = await approved_claim(client, team)
    lid = line["id"]
    for status in ("in_draft", "billed", "written_off"):
        await sql("UPDATE expenses SET billing_status = :s WHERE id = :i", s=status, i=lid)
        # The refusal comes before the reason is looked at.
        for body in ({"net": 3000}, {"net": 3000, "reason": "Fix"}):
            failure = await patch_line(client, owner, org, cid, lid, status=409, **body)
            assert failure["code"] == "EXPENSE_BILLED"
            assert failure["details"] == {"billing_status": status}
    assert (await line_row(lid))["net"] == 1000
    await sql("UPDATE expenses SET billing_status = 'unbilled' WHERE id = :i", i=lid)
    assert (await patch_line(client, owner, org, cid, lid, net=3000, reason="Fix"))["net"] == 3000
    assert (
        await count_rows("SELECT count(*) FROM expense_claim_events WHERE action = 'corrected'")
        == 1
    )


async def test_only_expense_manage_corrects_and_only_approved_claims(client):
    team = await expense_team(client)
    org, owner = team.org, team.owner
    await add_custom_role(
        org["id"],
        "Expense admin",
        ["organization.read", "member.read", "expense.log", "expense.manage"],
    )
    deputy = await add_member(org["id"], "Expense admin")
    staff, cid, line = await approved_claim(client, team)
    lid = line["id"]
    body = {"net": 1500, "reason": "Fix"}

    # The claimant of an approved claim cannot change it, nor can the approvers.
    locked = await patch_line(client, staff, org, cid, lid, status=409, **body)
    assert locked["code"] == "EXPENSE_CLAIM_LOCKED" and locked["details"] == {"status": "approved"}
    for role in ("manager", "billing", "viewer"):
        who = await team.member(role)
        response = await client.patch(
            claim_url(org, cid, f"/lines/{lid}"), headers=who.headers, json=body
        )
        assert response.status_code == 403, role
    assert (await line_row(lid))["net"] == 1000

    # Everything else about an approved claim stays closed, even to the Owner.
    for response in (
        await client.patch(claim_url(org, cid), headers=owner.headers, json={"title": "x"}),
        await client.post(
            claim_url(org, cid, "/lines"),
            headers=owner.headers,
            json={
                "expense_type_id": str(line["expense_type_id"]),
                "incurred_on": line["incurred_on"],
                "description": "More",
                "net": 100,
            },
        ),
        await client.delete(claim_url(org, cid, f"/lines/{lid}"), headers=owner.headers),
        await client.delete(claim_url(org, cid), headers=owner.headers),
    ):
        assert err(response, 409)["code"] == "EXPENSE_CLAIM_LOCKED"
    # What can be corrected is for those with expense.manage: the Owner and a custom role.
    assert (await patch_line(client, deputy, org, cid, lid, net=1500, reason="Fix"))["net"] == 1500
    assert (await patch_line(client, owner, org, cid, lid, net=1600, reason="Fix"))["net"] == 1600
    actors = [e["actor_user_id"] for e in await _events(client, owner, org, cid)]
    assert actors[-2:] == [str(deputy.id), str(owner.id)]

    # A claim that waits for a decision is locked for the Owner as well: they decide, not edit.
    waiting = await create_claim(client, staff, org, lines=1)
    await act(client, staff, org, waiting["id"], "submit")
    line_id = waiting["lines"][0]["id"]
    failure = await patch_line(client, owner, org, waiting["id"], line_id, status=409, **body)
    assert failure["code"] == "EXPENSE_CLAIM_LOCKED" and failure["details"] == {
        "status": "submitted"
    }


async def test_the_actions_tell_who_can_correct(client):
    team = await expense_team(client)
    org, owner = team.org, team.owner
    staff, cid, _ = await approved_claim(client, team)
    seen = {}
    for name, actor in (
        ("owner", owner),
        ("staff", staff),
        ("manager", await team.member("manager")),
    ):
        detail = ok_data(await client.get(claim_url(org, cid), headers=actor.headers))
        seen[name] = detail["actions"]["can_correct"]
    assert seen == {"owner": True, "staff": False, "manager": False}
    waiting = await create_claim(client, staff, org, lines=1)
    draft = ok_data(await client.get(claim_url(org, waiting["id"]), headers=owner.headers))
    assert draft["status"] == "draft" and draft["actions"]["can_correct"] is False
    await act(client, staff, org, waiting["id"], "submit")
    pending = ok_data(await client.get(claim_url(org, waiting["id"]), headers=owner.headers))
    assert pending["status"] == "submitted" and pending["actions"]["can_correct"] is False
    assert pending["actions"]["can_approve"] is True
