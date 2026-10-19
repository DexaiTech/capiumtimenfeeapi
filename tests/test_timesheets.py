"""Timesheets: submit, recall, approve, reject, reopen, the approvals queue and WIP gating."""

import asyncio
import uuid

from httpx import AsyncClient
from sqlalchemy import select

from app.db.models import Organization, TimeEntry
from app.db.session import get_sessionmaker
from app.services.time_entries import wip_eligibility_clause
from tests.domain_factories import make_client, practice, staff
from tests.factories import audit_entries
from tests.time_helpers import err, log_time, ok_data, org_url, set_entry, set_manager, set_practice


async def _sheet(client: AsyncClient, person, org, **body) -> str:
    entry = await log_time(client, person, org, **body)
    return str(entry["timesheet_id"])


async def _act(client, actor, org, sheet_id: str, action: str, expect: int = 200, json=None):
    response = await client.post(
        org_url(org, f"/timesheets/{sheet_id}/{action}"), headers=actor.headers, json=json
    )
    assert response.status_code == expect, response.text
    return response.json()["data"] if expect == 200 else response.json()["error"]


async def test_full_cycle(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org, full_name="Pat Person")
    manager = await staff(org, "manager")
    acme = await make_client(org)
    sheet_id = await _sheet(client, person, org, client_id=acme.id, minutes=120)
    await log_time(client, person, org, minutes=30, entry_date="2026-09-09")

    detail = ok_data(
        await client.get(org_url(org, f"/timesheets/{sheet_id}"), headers=person.headers)
    )
    assert detail["status"] == "open" and detail["user_name"] == "Pat Person"
    assert detail["entry_count"] == 2 and detail["total_minutes"] == 150
    assert detail["billable_minutes"] == 120 and detail["non_billable_minutes"] == 30
    assert detail["week_start"] == "2026-09-07" and detail["week_end"] == "2026-09-13"
    assert [d["minutes"] for d in detail["day_totals"]] == [120, 0, 30, 0, 0, 0, 0]
    assert len(detail["entries"]) == 2

    submitted = await _act(client, person, org, sheet_id, "submit")
    assert submitted["status"] == "submitted" and submitted["submitted_at"]
    assert all(e["is_locked"] for e in submitted["entries"])
    # Locked for edits now.
    await log_time(client, person, org, status=409)
    rejected = await _act(
        client, manager, org, sheet_id, "reject", json={"decision_note": "Add detail"}
    )
    assert rejected["status"] == "rejected" and rejected["decision_note"] == "Add detail"
    assert rejected["decided_by"] == str(manager.id)
    await log_time(client, person, org, minutes=15)  # editable again
    resubmitted = await _act(client, person, org, sheet_id, "submit")
    assert resubmitted["decision_note"] is None and resubmitted["entry_count"] == 3
    approved = await _act(
        client, manager, org, sheet_id, "approve", json={"decision_note": "Thanks"}
    )
    assert approved["status"] == "approved" and approved["decided_at"]
    assert approved["decision_note"] == "Thanks"
    reopened = await _act(client, manager, org, sheet_id, "reopen")
    assert reopened["status"] == "open" and reopened["decided_by"] is None

    actions = [
        e["action"] for e in await audit_entries(org["id"]) if e["target_type"] == "timesheet"
    ]
    assert actions == [
        "timesheet.submitted",
        "timesheet.rejected",
        "timesheet.submitted",
        "timesheet.approved",
        "timesheet.reopened",
    ]
    log = await audit_entries(org["id"], "timesheet.rejected")
    assert log[0]["metadata"]["decision_note"] == "Add detail"
    assert log[0]["metadata"]["user_name"] == "Pat Person"
    assert log[0]["metadata"]["total_minutes"] == 150


async def test_invalid_transitions(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    manager = await staff(org, "manager")
    sheet_id = await _sheet(client, person, org)
    for actor, action in ((person, "recall"), (manager, "approve"), (manager, "reopen")):
        error = await _act(client, actor, org, sheet_id, action, 409)
        assert error["code"] == "INVALID_STATUS_TRANSITION"
    error = await _act(client, manager, org, sheet_id, "reject", 409, json={"decision_note": "x"})
    assert error["code"] == "INVALID_STATUS_TRANSITION"
    await _act(client, person, org, sheet_id, "submit")
    assert (await _act(client, person, org, sheet_id, "submit", 409))[
        "code"
    ] == "INVALID_STATUS_TRANSITION"
    recalled = await _act(client, person, org, sheet_id, "recall")
    assert recalled["status"] == "open" and recalled["submitted_at"] is None
    await _act(client, person, org, sheet_id, "submit")
    await _act(client, manager, org, sheet_id, "approve")
    for actor, action in ((person, "submit"), (person, "recall"), (manager, "approve")):
        await _act(client, actor, org, sheet_id, action, 409)
    await _act(client, manager, org, sheet_id, "reject", 409, json={"decision_note": "x"})


async def test_submit_rules(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    other = await staff(org)
    entry = await log_time(client, person, org)
    sheet_id = str(entry["timesheet_id"])
    # Only the owner submits.
    manager = await staff(org, "manager")
    await _act(client, manager, org, sheet_id, "submit", 403)
    await _act(client, other, org, sheet_id, "submit", 403)
    # Empty sheets cannot be submitted.
    await client.delete(org_url(org, f"/time-entries/{entry['id']}"), headers=person.headers)
    assert (await _act(client, person, org, sheet_id, "submit", 422))["code"] == "TIMESHEET_EMPTY"


async def test_reject_needs_note(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    manager = await staff(org, "manager")
    sheet_id = await _sheet(client, person, org)
    await _act(client, person, org, sheet_id, "submit")
    await _act(client, manager, org, sheet_id, "reject", 422, json={})
    await _act(client, manager, org, sheet_id, "reject", 422, json={"decision_note": "   "})
    await _act(client, manager, org, sheet_id, "reject", 422)


async def test_self_approval(client: AsyncClient) -> None:
    owner, org = await practice(client)
    manager = await staff(org, "manager")
    other_manager = await staff(org, "manager")
    sheet_id = await _sheet(client, manager, org)
    await _act(client, manager, org, sheet_id, "submit")
    error = await _act(client, manager, org, sheet_id, "approve", 403)
    assert error["code"] == "SELF_APPROVAL_FORBIDDEN"
    await _act(client, manager, org, sheet_id, "reject", 403, json={"decision_note": "x"})
    response = await client.post(
        org_url(org, "/approvals/bulk-approve"),
        headers=manager.headers,
        json={"timesheet_ids": [sheet_id]},
    )
    assert err(response, 403)["code"] == "SELF_APPROVAL_FORBIDDEN"
    await _act(client, other_manager, org, sheet_id, "approve")
    await _act(client, manager, org, sheet_id, "reopen", 403)
    # Owners/Admins (organization.update) may decide on their own sheet.
    own = await _sheet(client, owner, org)
    await _act(client, owner, org, own, "submit")
    await _act(client, owner, org, own, "approve")
    await _act(client, owner, org, own, "reopen")


async def test_approve_permissions(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    sheet_id = await _sheet(client, person, org)
    await _act(client, person, org, sheet_id, "submit")
    for role in ("staff", "billing", "viewer"):
        actor = await staff(org, role)
        await _act(client, actor, org, sheet_id, "approve", 403)
        await _act(client, actor, org, sheet_id, "reject", 403, json={"decision_note": "x"})
        await _act(client, actor, org, sheet_id, "reopen", 403)
        response = await client.post(
            org_url(org, "/approvals/bulk-approve"),
            headers=actor.headers,
            json={"timesheet_ids": [sheet_id]},
        )
        assert response.status_code == 403
    for role in ("partner", "admin"):
        await staff(org, role)
    await _act(client, owner, org, sheet_id, "approve")


async def test_reopen_blocked_by_billed_time(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    manager = await staff(org, "manager")
    acme = await make_client(org)
    entry = await log_time(client, person, org, client_id=acme.id)
    sheet_id = str(entry["timesheet_id"])
    await _act(client, person, org, sheet_id, "submit")
    await _act(client, manager, org, sheet_id, "approve")
    for billing_status in ("in_draft", "billed", "written_off"):
        await set_entry(entry["id"], billing_status=billing_status)
        error = await _act(client, manager, org, sheet_id, "reopen", 409)
        assert error["code"] == "TIMESHEET_HAS_BILLED_TIME"
    await set_entry(entry["id"], billing_status="unbilled")
    await _act(client, manager, org, sheet_id, "reopen")


async def test_visibility(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    lead = await staff(org)  # a staff member who manages the person
    stranger = await staff(org)
    viewer = await staff(org, "viewer")
    await set_manager(org, person, lead)
    sheet_id = await _sheet(client, person, org)
    url = org_url(org, f"/timesheets/{sheet_id}")
    assert (await client.get(url, headers=stranger.headers)).status_code == 403
    assert (await client.get(url, headers=lead.headers)).status_code == 200
    assert (await client.get(url, headers=viewer.headers)).status_code == 200
    detail = ok_data(await client.get(url, headers=lead.headers))
    assert detail["entries"][0]["cost_rate"] is None  # no rate.read
    detail = ok_data(await client.get(url, headers=owner.headers))
    assert detail["entries"][0]["cost_rate"] is not None
    # Lists
    base = org_url(org, "/timesheets")
    params = {"user_id": str(person.id)}
    assert (await client.get(base, headers=stranger.headers, params=params)).status_code == 403
    assert len(ok_data(await client.get(base, headers=lead.headers, params=params))) == 1
    lead_all = ok_data(await client.get(base, headers=lead.headers, params={"user_id": "all"}))
    assert [s["id"] for s in lead_all] == [sheet_id]
    stranger_all = ok_data(
        await client.get(base, headers=stranger.headers, params={"user_id": "all"})
    )
    assert stranger_all == []
    # Other practices: 404.
    other_owner, other = await practice(client, "Other & Co")
    assert (await client.get(url, headers=other_owner.headers)).status_code == 404
    their_url = org_url(other, f"/timesheets/{sheet_id}")
    assert (await client.get(their_url, headers=other_owner.headers)).status_code == 404
    response = await client.post(f"{their_url}/approve", headers=other_owner.headers)
    assert response.status_code == 404
    assert (
        await client.get(org_url(org, f"/timesheets/{uuid.uuid4()}"), headers=owner.headers)
    ).status_code == 404


async def test_list_filters_and_pagination(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    weeks = ["2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28"]
    ids = [await _sheet(client, person, org, entry_date=w) for w in weeks]
    await _act(client, person, org, ids[1], "submit")
    base = org_url(org, "/timesheets")

    async def listed(**params):
        return [
            s["id"] for s in ok_data(await client.get(base, headers=person.headers, params=params))
        ]

    assert await listed() == ids[::-1]
    assert await listed(**{"from": "2026-09-14", "to": "2026-09-21"}) == [ids[2], ids[1]]
    assert await listed(status="submitted") == [ids[1]]
    assert await listed(status=["open"]) == [ids[3], ids[2], ids[0]]
    seen, cursor = [], None
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        body = (await client.get(base, headers=person.headers, params=params)).json()
        seen += [s["id"] for s in body["data"]]
        cursor = body["meta"]["next_cursor"]
        if not cursor:
            break
    assert seen == ids[::-1]
    assert (
        await client.get(base, headers=person.headers, params={"user_id": "x"})
    ).status_code == 422


async def test_approvals_queue_and_bulk_approve(client: AsyncClient) -> None:
    owner, org = await practice(client)
    lead = await staff(org, "manager")
    a, b, c = await staff(org), await staff(org), await staff(org)
    await set_manager(org, a, lead)
    await set_manager(org, b, lead)
    sheets = {}
    for person in (a, b, c):
        sheets[person.id] = await _sheet(client, person, org)
        await _act(client, person, org, sheets[person.id], "submit")
    open_sheet = await _sheet(client, a, org, entry_date="2026-09-14")  # not submitted

    queue = ok_data(await client.get(org_url(org, "/approvals"), headers=lead.headers))
    # Managers hold time.read_all, so they see everyone's; a staff lead sees only theirs.
    assert {s["id"] for s in queue} == {sheets[a.id], sheets[b.id], sheets[c.id]}
    staff_lead = await staff(org)
    await set_manager(org, c, staff_lead)
    queue = ok_data(await client.get(org_url(org, "/approvals"), headers=staff_lead.headers))
    assert [s["id"] for s in queue] == [sheets[c.id]]
    nobody = await staff(org)
    assert ok_data(await client.get(org_url(org, "/approvals"), headers=nobody.headers)) == []

    url = org_url(org, "/approvals/bulk-approve")
    # Atomic: one bad id (not submitted) rolls back all.
    response = await client.post(
        url, headers=lead.headers, json={"timesheet_ids": [sheets[a.id], open_sheet]}
    )
    assert err(response, 409)["code"] == "INVALID_STATUS_TRANSITION"
    response = await client.post(
        url, headers=lead.headers, json={"timesheet_ids": [sheets[a.id], str(uuid.uuid4())]}
    )
    assert err(response, 404)
    still = ok_data(
        await client.get(org_url(org, f"/timesheets/{sheets[a.id]}"), headers=lead.headers)
    )
    assert still["status"] == "submitted"
    approved = ok_data(
        await client.post(
            url,
            headers=lead.headers,
            json={"timesheet_ids": [sheets[a.id], sheets[b.id], sheets[a.id]]},
        )
    )
    assert [s["status"] for s in approved] == ["approved", "approved"]
    assert len(await audit_entries(org["id"], "timesheet.approved")) == 2
    queue = ok_data(await client.get(org_url(org, "/approvals"), headers=owner.headers))
    assert [s["id"] for s in queue] == [sheets[c.id]]
    assert (
        await client.post(url, headers=lead.headers, json={"timesheet_ids": []})
    ).status_code == 422


async def test_parallel_decisions(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    m1, m2 = await staff(org, "manager"), await staff(org, "manager")
    sheet_id = await _sheet(client, person, org)
    await _act(client, person, org, sheet_id, "submit")
    responses = await asyncio.gather(
        client.post(org_url(org, f"/timesheets/{sheet_id}/approve"), headers=m1.headers),
        client.post(
            org_url(org, f"/timesheets/{sheet_id}/reject"),
            headers=m2.headers,
            json={"decision_note": "No"},
        ),
        client.post(org_url(org, f"/timesheets/{sheet_id}/recall"), headers=person.headers),
    )
    assert sorted(r.status_code for r in responses) == [200, 409, 409]
    assert len([e for e in await audit_entries(org["id"]) if e["target_type"] == "timesheet"]) == 2


async def test_entry_edit_and_submit_race(client: AsyncClient) -> None:
    """Submit and an edit on the same sheet serialise: either the edit lands before the
    submit, or it is refused with TIME_LOCKED."""
    _, org = await practice(client)
    await set_practice(org, time_increment_minutes=1)
    person = await staff(org)
    entry = await log_time(client, person, org, minutes=10)
    sheet_id = str(entry["timesheet_id"])
    edit, submit = await asyncio.gather(
        client.patch(
            org_url(org, f"/time-entries/{entry['id']}"),
            headers=person.headers,
            json={"minutes": 20},
        ),
        client.post(org_url(org, f"/timesheets/{sheet_id}/submit"), headers=person.headers),
    )
    assert submit.status_code == 200
    detail = ok_data(
        await client.get(org_url(org, f"/timesheets/{sheet_id}"), headers=person.headers)
    )
    if edit.status_code == 200:
        assert detail["total_minutes"] == 20
    else:
        assert edit.status_code == 409 and edit.json()["error"]["code"] == "TIME_LOCKED"
        assert detail["total_minutes"] == 10


async def test_wip_eligibility_clause(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    manager = await staff(org, "manager")
    acme = await make_client(org)
    approved = await log_time(client, person, org, client_id=acme.id, entry_date="2026-09-07")
    await log_time(client, person, org, entry_date="2026-09-07")  # non-billable
    open_entry = await log_time(client, person, org, client_id=acme.id, entry_date="2026-09-14")
    billed = await log_time(client, person, org, client_id=acme.id, entry_date="2026-09-08")
    await set_entry(billed["id"], billing_status="billed")
    await _act(client, person, org, str(approved["timesheet_id"]), "submit")
    await _act(client, manager, org, str(approved["timesheet_id"]), "approve")

    async def eligible() -> set[str]:
        async with get_sessionmaker()() as session:
            organization = await session.get(Organization, uuid.UUID(org["id"]))
            assert organization is not None
            rows = await session.scalars(
                select(TimeEntry.id).where(
                    TimeEntry.organization_id == organization.id,
                    wip_eligibility_clause(organization),
                )
            )
            return {str(r) for r in rows}

    assert await eligible() == {approved["id"]}
    await set_practice(org, require_timesheet_approval=False)
    assert await eligible() == {approved["id"], open_entry["id"]}
