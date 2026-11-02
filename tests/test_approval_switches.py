"""Ticket APT53: a practice can switch each approval flow off (timesheets, expense claims, WIP
adjustments, invoices). Off, the flow is skipped end to end; switching it off approves what was
waiting (unless its rules refuse it); switching it on again needs approval for new work
(``app/services/approval_switches.py``)."""

from datetime import timedelta

from httpx import AsyncClient

from tests.billing_helpers import draft, issue, issued
from tests.dashboard_page_helpers import get_page
from tests.domain_factories import make_client, make_time_entry, practice, staff
from tests.expense_helpers import act, add_line, create_claim
from tests.factories import Actor, audit_entries, error
from tests.notification_helpers import inbox
from tests.period_lock_helpers import set_lock, today
from tests.test_time_approval_exempt import eligible
from tests.time_helpers import log_time, ok_data, org_url, set_practice

SWITCHES = (
    "require_timesheet_approval",
    "expense_approval_required",
    "wip_adjustment_approval_required",
    "invoice_approval_required",
)


async def switch(client: AsyncClient, owner: Actor, org: dict, **values: bool) -> dict:
    response = await client.patch(org_url(org, ""), headers=owner.headers, json=values)
    data = ok_data(response)
    assert {name: data[name] for name in values} == values
    return data


async def settled(org: dict) -> dict:
    """The ``approvals_settled`` of the latest settings change."""
    entries = await audit_entries(org["id"], "organization.updated")
    return entries[-1]["metadata"].get("approvals_settled", {})


async def submit_week(client: AsyncClient, actor: Actor, org: dict, entry: dict) -> dict:
    response = await client.post(
        org_url(org, f"/timesheets/{entry['timesheet_id']}/submit"), headers=actor.headers
    )
    return ok_data(response)


async def queue(client: AsyncClient, actor: Actor, org: dict, path: str) -> list[dict]:
    return ok_data(await client.get(org_url(org, path), headers=actor.headers))


# --- The settings -------------------------------------------------------------------------------


async def test_every_approval_flow_is_on_by_default_except_invoices(client):
    _, org = await practice(client)
    assert {name: org[name] for name in SWITCHES} == {
        "require_timesheet_approval": True,
        "expense_approval_required": True,
        "wip_adjustment_approval_required": True,
        "invoice_approval_required": False,
    }


async def test_switching_needs_organization_update_and_is_audited(client):
    owner, org = await practice(client)
    manager = await staff(org, "manager")
    refused = await client.patch(
        org_url(org, ""), headers=manager.headers, json={"expense_approval_required": False}
    )
    assert refused.status_code == 403, refused.text
    await switch(client, owner, org, expense_approval_required=False)
    [entry] = await audit_entries(org["id"], "organization.updated")
    assert entry["actor_user_id"] == str(owner.id)
    assert entry["metadata"]["changes"] == {"expense_approval_required": [True, False]}
    assert entry["metadata"]["approvals_settled"] == {"expense_claims": {"approved": 0, "left": 0}}


# --- Timesheets ---------------------------------------------------------------------------------


async def test_with_timesheet_approval_off_nobody_approves_time(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    person = await staff(org, "staff")
    await switch(client, owner, org, require_timesheet_approval=False)
    entry = await log_time(client, person, org, client_id=acme.id)
    # Billable as it is recorded, and submitting the week approves it at once.
    assert await eligible(org) == {entry["id"]}
    sheet = await submit_week(client, person, org, entry)
    assert sheet["status"] == "approved" and sheet["decision_note"] == "Approval not required"
    assert await queue(client, owner, org, "/approvals") == []
    assert [n["type"] for n in await inbox(client, owner, org)] == []
    # The dashboard has no timesheet approvals to offer.
    pending = await get_page(client, owner, org, "pending-approvals")
    assert [kind["kind"] for kind in pending["kinds"]] == ["expense"]


async def test_switching_timesheet_approval_off_approves_the_weeks_waiting(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    person = await staff(org, "staff")
    entry = await log_time(client, person, org, client_id=acme.id)
    assert (await submit_week(client, person, org, entry))["status"] == "submitted"
    assert await eligible(org) == set()

    await switch(client, owner, org, require_timesheet_approval=False)
    sheet = ok_data(
        await client.get(
            org_url(org, f"/timesheets/{entry['timesheet_id']}"), headers=owner.headers
        )
    )
    assert sheet["status"] == "approved"
    assert sheet["decision_note"] == "Approval switched off"
    assert sheet["decided_by"] == str(owner.id)
    assert await queue(client, owner, org, "/approvals") == []
    assert await settled(org) == {"timesheets": {"approved": 1, "left": 0}}
    [approval] = await audit_entries(org["id"], "timesheet.approved")
    assert approval["actor_user_id"] == str(owner.id)
    assert approval["metadata"]["decision_note"] == "Approval switched off"


async def test_switching_timesheet_approval_back_on_needs_approval_again(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    person = await staff(org, "staff")
    await switch(client, owner, org, require_timesheet_approval=False)
    entry = await log_time(client, person, org, client_id=acme.id)
    assert await eligible(org) == {entry["id"]}

    await switch(client, owner, org, require_timesheet_approval=True)
    # The open week needs approval again; nothing was decided for it.
    assert await eligible(org) == set()
    assert (await submit_week(client, person, org, entry))["status"] == "submitted"
    waiting = await queue(client, owner, org, "/approvals")
    assert [sheet["id"] for sheet in waiting] == [entry["timesheet_id"]]
    assert await settled(org) == {}


async def test_no_approver_is_reminded_when_timesheets_are_not_approved(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    person = await staff(org, "staff")
    entry = await log_time(client, person, org, client_id=acme.id)
    await submit_week(client, person, org, entry)
    # A week left waiting from before (set straight in the table, so nothing settles it).
    await set_practice(org, require_timesheet_approval=False)
    result = ok_data(
        await client.post(
            org_url(org, "/timesheets/reminders/run"),
            headers=owner.headers,
            json={"week_start": entry["entry_date"]},
        )
    )
    assert result["sheets_waiting"] == 0 and result["approvers_reminded"] == 0


# --- Expense claims -----------------------------------------------------------------------------


async def test_with_expense_approval_off_a_submitted_claim_is_approved_at_once(client):
    owner, org = await practice(client)
    person = await staff(org, "staff")
    await switch(client, owner, org, expense_approval_required=False)
    claim = await create_claim(client, person, org, lines=1)
    done = await act(client, person, org, claim["id"], "submit")
    assert done["status"] == "approved"
    assert done["decision_note"] == "Approval not required"
    assert [event["action"] for event in done["events"]] == ["submitted", "approved"]
    assert await queue(client, owner, org, "/expense-approvals") == []
    assert [n["type"] for n in await inbox(client, owner, org)] == []
    assert [n["type"] for n in await inbox(client, person, org)] == []
    pending = await get_page(client, owner, org, "pending-approvals")
    assert [kind["kind"] for kind in pending["kinds"]] == ["timesheet"]


async def test_switching_expense_approval_off_approves_the_claims_waiting(client):
    owner, org = await practice(client)
    person = await staff(org, "staff")
    lock = today(org) - timedelta(days=10)
    waiting = await create_claim(client, person, org)
    await add_line(client, person, org, waiting["id"], incurred_on=str(today(org)))
    await act(client, person, org, waiting["id"], "submit")
    old = await create_claim(client, person, org)
    await add_line(client, person, org, old["id"], incurred_on=str(lock))
    await act(client, person, org, old["id"], "submit")
    await set_lock(org, lock)

    await switch(client, owner, org, expense_approval_required=False)
    claims = {claim["id"]: claim for claim in await queue(client, owner, org, "/expense-claims")}
    assert claims[waiting["id"]]["status"] == "approved"
    # A claim dated in a locked period is left for a person to decide.
    assert claims[old["id"]]["status"] == "submitted"
    assert await settled(org) == {"expense_claims": {"approved": 1, "left": 1}}
    [entry] = await audit_entries(org["id"], "expense_claim.approved")
    assert entry["target_id"] == waiting["id"]
    assert entry["metadata"]["note"] == "Approval switched off"
    # The claimant hears of it, as of any approval.
    assert [n["type"] for n in await inbox(client, person, org)] == ["expense_approved"]


async def test_switching_expense_approval_back_on_needs_approval_again(client):
    owner, org = await practice(client)
    person = await staff(org, "staff")
    await switch(client, owner, org, expense_approval_required=False)
    await switch(client, owner, org, expense_approval_required=True)
    claim = await create_claim(client, person, org, lines=1)
    assert (await act(client, person, org, claim["id"], "submit"))["status"] == "submitted"
    waiting = await queue(client, owner, org, "/expense-approvals")
    assert [row["id"] for row in waiting] == [claim["id"]]


# --- WIP adjustments ----------------------------------------------------------------------------


async def adjust(client: AsyncClient, actor: Actor, org: dict, client_id, kind: str, amount: int):
    response = await client.post(
        org_url(org, "/wip/adjustments"),
        headers=actor.headers,
        json={"client_id": str(client_id), "kind": kind, "amount": amount, "reason": "Review"},
    )
    return ok_data(response, 201)


async def test_with_wip_approval_off_every_adjustment_takes_effect_at_once(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    await make_time_entry(org, owner, client=acme, minutes=600, charge_rate=6000)
    await switch(client, owner, org, wip_adjustment_approval_required=False)
    # £550 of a £600 WIP: above both the £500 and the 10% threshold.
    row = await adjust(client, owner, org, acme.id, "write_off", 55_000)
    assert row["status"] == "approved" and row["approval_required"] is False
    assert row["in_force"] is True

    await switch(client, owner, org, wip_adjustment_approval_required=True)
    row = await adjust(client, owner, org, acme.id, "write_on", 60_000)
    assert row["status"] == "pending" and row["approval_required"] is True


async def test_switching_wip_approval_off_approves_the_adjustments_pending(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    await make_time_entry(org, owner, client=acme, minutes=600, charge_rate=6000)
    ok_data(
        await client.patch(
            org_url(org, ""), headers=owner.headers, json={"wip_adjustment_approval_threshold": 0}
        )
    )
    first = await adjust(client, owner, org, acme.id, "write_off", 40_000)
    second = await adjust(client, owner, org, acme.id, "write_off", 40_000)
    assert first["status"] == second["status"] == "pending"

    await switch(client, owner, org, wip_adjustment_approval_required=False)
    rows = {row["id"]: row for row in await queue(client, owner, org, "/wip/adjustments?limit=10")}
    assert rows[first["id"]]["status"] == "approved"
    assert rows[first["id"]]["decision_note"] == "Approval switched off"
    # Only £200 of WIP is left: the second write-off waits for a person.
    assert rows[second["id"]]["status"] == "pending"
    assert await settled(org) == {"wip_adjustments": {"approved": 1, "left": 1}}
    [entry] = await audit_entries(org["id"], "wip.adjustment_approved")
    assert entry["target_id"] == first["id"]


# --- Invoices -----------------------------------------------------------------------------------


async def test_switching_invoice_approval_off_approves_the_invoices_waiting(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    await switch(client, owner, org, invoice_approval_required=True)
    entry = await make_time_entry(org, owner, client=acme)
    invoice = await draft(client, owner, org, acme.id, time=[entry])
    submitted = await client.post(
        org_url(org, f"/invoices/{invoice['id']}/submit"), headers=owner.headers, json={}
    )
    assert ok_data(submitted)["status"] == "awaiting_approval"
    refused = await issue(client, owner, org, invoice["id"])
    assert refused.status_code == 409, refused.text

    await switch(client, owner, org, invoice_approval_required=False)
    assert await settled(org) == {"invoices": {"approved": 1, "left": 0}}
    detail = ok_data(
        await client.get(org_url(org, f"/invoices/{invoice['id']}"), headers=owner.headers)
    )
    assert detail["status"] == "approved"
    assert [step["action"] for step in detail["approvals"]] == ["submitted", "approved"]
    # Approved, not issued: it is issued as usual.
    assert (await issued(client, owner, org, invoice["id"]))["status"] == "issued"


async def test_with_invoice_approval_off_an_invoice_is_issued_straight_away(client):
    owner, org = await practice(client)
    acme = await make_client(org, "Acme Ltd")
    entry = await make_time_entry(org, owner, client=acme)
    invoice = await draft(client, owner, org, acme.id, time=[entry])
    submitted = await client.post(
        org_url(org, f"/invoices/{invoice['id']}/submit"), headers=owner.headers, json={}
    )
    assert submitted.status_code == 409, submitted.text
    assert error(submitted)["code"] == "APPROVAL_NOT_REQUIRED"
    assert (await issued(client, owner, org, invoice["id"]))["status"] == "issued"


async def test_settling_runs_only_for_a_switch_turned_off(client):
    owner, org = await practice(client)
    await switch(client, owner, org, invoice_approval_required=True)
    assert await settled(org) == {}
    await switch(client, owner, org, require_timesheet_approval=True)
    assert len(await audit_entries(org["id"], "organization.updated")) == 1
