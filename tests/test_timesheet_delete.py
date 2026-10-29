"""Deleting a timesheet, and who may change whose time (APT48)."""

from httpx import AsyncClient

from tests.domain_factories import make_client, practice, staff
from tests.factories import audit_entries
from tests.test_member_permissions import put
from tests.time_helpers import entry_row, err, log_time, ok_data, org_url, set_entry


async def _post(client: AsyncClient, actor, org, sheet_id: str, action: str, **kwargs):
    return await client.post(
        org_url(org, f"/timesheets/{sheet_id}/{action}"), headers=actor.headers, **kwargs
    )


async def _delete(client: AsyncClient, actor, org, sheet_id: str):
    return await client.delete(org_url(org, f"/timesheets/{sheet_id}"), headers=actor.headers)


async def test_the_owner_deletes_an_open_sheet_with_its_time(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    first = await log_time(client, person, org, client_id=acme.id, minutes=90)
    second = await log_time(client, person, org, minutes=30, entry_date="2026-09-09")
    sheet_id = str(first["timesheet_id"])
    other_week = await log_time(client, person, org, minutes=15, entry_date="2026-09-16")

    response = await _delete(client, person, org, sheet_id)
    assert response.status_code == 200, response.text
    assert (
        await client.get(org_url(org, f"/timesheets/{sheet_id}"), headers=person.headers)
    ).status_code == 404
    for gone in (first, second):
        url = org_url(org, f"/time-entries/{gone['id']}")
        assert (await client.get(url, headers=person.headers)).status_code == 404
    kept = org_url(org, f"/time-entries/{other_week['id']}")
    assert (await client.get(kept, headers=person.headers)).status_code == 200

    [deleted] = await audit_entries(org["id"], "timesheet.deleted")
    assert deleted["target_id"] == sheet_id and deleted["actor_user_id"] == str(person.id)
    assert deleted["metadata"]["entry_count"] == 2 and deleted["metadata"]["total_minutes"] == 120
    assert len(await audit_entries(org["id"], "time_entry.deleted")) == 2
    assert err(await _delete(client, person, org, sheet_id), 404)


async def test_a_rejected_sheet_can_be_deleted_but_a_decided_one_cannot(
    client: AsyncClient,
) -> None:
    _, org = await practice(client)
    person = await staff(org)
    manager = await staff(org, "manager")
    entry = await log_time(client, person, org)
    sheet_id = str(entry["timesheet_id"])

    assert (await _post(client, person, org, sheet_id, "submit")).status_code == 200
    for status in ("submitted", "approved"):
        error = err(await _delete(client, person, org, sheet_id), 409)
        assert error["code"] == "INVALID_STATUS_TRANSITION", status
        if status == "submitted":
            ok_data(await _post(client, manager, org, sheet_id, "approve"))
    assert (await entry_row(entry["id"])).timesheet_id is not None

    ok_data(await _post(client, manager, org, sheet_id, "reopen"))
    ok_data(await _post(client, person, org, sheet_id, "submit"))
    ok_data(
        await _post(client, manager, org, sheet_id, "reject", json={"decision_note": "Add detail"})
    )
    assert (await _delete(client, person, org, sheet_id)).status_code == 200
    assert (
        await client.get(org_url(org, f"/time-entries/{entry['id']}"), headers=person.headers)
    ).status_code == 404


async def test_a_sheet_with_billed_time_is_not_deleted(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    billed = await log_time(client, person, org, client_id=acme.id, is_billable=True)
    await log_time(client, person, org, minutes=30, entry_date="2026-09-09")
    await set_entry(billed["id"], billing_status="in_draft")

    error = err(await _delete(client, person, org, str(billed["timesheet_id"])), 409)
    assert error["code"] == "TIMESHEET_HAS_BILLED_TIME"
    assert (await entry_row(billed["id"])).billing_status == "in_draft"
    # All or nothing: the unbilled entry is still there.
    url = org_url(org, "/time-entries")
    listed = ok_data(await client.get(url, headers=person.headers))
    assert len(listed) == 2
    assert await audit_entries(org["id"], "timesheet.deleted") == []


async def test_only_people_who_manage_all_time_delete_someone_elses_sheet(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    colleague = await staff(org)
    manager = await staff(org, "manager")
    sheet_id = str((await log_time(client, person, org))["timesheet_id"])

    err(await _delete(client, colleague, org, sheet_id), 403)
    # Approving without time.manage_all does not allow it either.
    await put(client, owner, org, colleague, grants=["timesheet.approve"])
    err(await _delete(client, colleague, org, sheet_id), 403)
    assert (await _delete(client, manager, org, sheet_id)).status_code == 200


async def test_approving_alone_does_not_let_a_person_change_anyone_elses_time(
    client: AsyncClient,
) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    approver = await staff(org)
    acme = await make_client(org)
    other = await make_client(org)
    entry = await log_time(client, person, org, client_id=acme.id)
    url = org_url(org, f"/time-entries/{entry['id']}")
    await put(client, owner, org, approver, grants=["timesheet.approve", "time.read_all"])

    patch = await client.patch(url, headers=approver.headers, json={"client_id": str(other.id)})
    assert patch.status_code == 403, patch.text
    assert (await client.delete(url, headers=approver.headers)).status_code == 403
    await log_time(client, approver, org, status=403, user_id=str(person.id))
    assert str((await entry_row(entry["id"])).client_id) == str(acme.id)

    # They can still decide on the sheet, and the owner then cannot move the locked time.
    sheet_id = str(entry["timesheet_id"])
    ok_data(await _post(client, person, org, sheet_id, "submit"))
    ok_data(await _post(client, approver, org, sheet_id, "approve"))
    locked = await client.patch(url, headers=person.headers, json={"client_id": str(other.id)})
    assert err(locked, 409)["code"] == "TIME_LOCKED"
    assert (await client.delete(url, headers=person.headers)).status_code == 409
    ok_data(await _post(client, approver, org, sheet_id, "reopen"))
    moved = await client.patch(url, headers=person.headers, json={"client_id": str(other.id)})
    assert moved.status_code == 200, moved.text
    assert (await client.delete(url, headers=person.headers)).status_code == 200
