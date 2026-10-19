"""Time entries: the internal note and the job's service on entry responses."""

import json

from httpx import AsyncClient

from tests.domain_factories import make_client, make_job, practice, service_id, staff
from tests.factories import audit_entries
from tests.time_gap_helpers import day
from tests.time_helpers import err, log_time, ok_data, org_url, set_manager


async def test_the_note_is_stored_trimmed_edited_and_cleared(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    entry = await log_time(client, person, org, internal_note="  Ask Sam about the loan  ")
    assert entry["internal_note"] == "Ask Sam about the loan"
    url = org_url(org, f"/time-entries/{entry['id']}")

    edited = ok_data(
        await client.patch(url, headers=person.headers, json={"internal_note": "Done"})
    )
    assert edited["internal_note"] == "Done"
    # Leaving it out keeps it.
    other = ok_data(await client.patch(url, headers=person.headers, json={"description": "x"}))
    assert other["internal_note"] == "Done" and other["description"] == "x"
    for blank in (None, "", "   "):
        await client.patch(url, headers=person.headers, json={"internal_note": "Back again"})
        cleared = ok_data(
            await client.patch(url, headers=person.headers, json={"internal_note": blank})
        )
        assert cleared["internal_note"] is None
    # A blank note is never stored as an empty string.
    created = await log_time(client, person, org, internal_note="  ")
    assert created["internal_note"] is None


async def test_the_note_is_limited_to_2000_characters(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    longest = "n" * 2000
    entry = await log_time(client, person, org, internal_note=longest)
    assert entry["internal_note"] == longest
    error = await log_time(client, person, org, status=422, internal_note=longest + "n")
    assert error["details"][0]["field"] == "internal_note"
    response = await client.patch(
        org_url(org, f"/time-entries/{entry['id']}"),
        headers=person.headers,
        json={"internal_note": longest + "n"},
    )
    assert err(response, 422)["details"][0]["field"] == "internal_note"


async def test_bulk_entries_take_notes(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    response = await client.post(
        org_url(org, "/time-entries/bulk"),
        headers=person.headers,
        json={
            "entries": [
                {"entry_date": day(0), "minutes": 60, "internal_note": "first"},
                {"entry_date": day(1), "minutes": 30},
            ]
        },
    )
    notes = [e["internal_note"] for e in ok_data(response, 201)]
    assert notes == ["first", None]


async def test_who_sees_the_note(client: AsyncClient) -> None:
    owner, org = await practice(client)
    person = await staff(org)
    lead = await staff(org)  # plain staff who approves the person's timesheets
    manager = await staff(org, "manager")  # time.read_all and time.manage_all
    viewer = await staff(org, "viewer")  # time.read_all
    await set_manager(org, person, lead)
    entry = await log_time(client, person, org, internal_note="Private", description="Public")
    url = org_url(org, f"/time-entries/{entry['id']}")
    for actor in (person, owner, manager, viewer):
        assert ok_data(await client.get(url, headers=actor.headers))["internal_note"] == "Private"
    listed = ok_data(
        await client.get(
            org_url(org, "/time-entries"), headers=viewer.headers, params={"user_id": "all"}
        )
    )
    assert [e["internal_note"] for e in listed] == ["Private"]

    # The person's manager reads the timesheet and the day but not the note.
    sheet_url = org_url(org, f"/timesheets/{entry['timesheet_id']}")
    seen = ok_data(await client.get(sheet_url, headers=lead.headers))["entries"][0]
    assert seen["internal_note"] is None and seen["description"] == "Public"
    assert (
        ok_data(await client.get(sheet_url, headers=manager.headers))["entries"][0]["internal_note"]
        == "Private"
    )
    day_view = await client.get(
        org_url(org, "/time-entries/day"),
        headers=lead.headers,
        params={"date": day(0), "user_id": str(person.id)},
    )
    assert ok_data(day_view)["entries"][0]["internal_note"] is None

    # Someone with time.manage_all who logs for the person sees what they wrote.
    behalf = await log_time(
        client, manager, org, user_id=person.id, internal_note="By the manager", minutes=30
    )
    assert behalf["internal_note"] == "By the manager" and behalf["created_by"] == str(manager.id)


async def test_the_audit_trail_says_a_note_changed_but_never_what_it_says(
    client: AsyncClient,
) -> None:
    _, org = await practice(client)
    person = await staff(org)
    entry = await log_time(client, person, org, description="Work")
    url = org_url(org, f"/time-entries/{entry['id']}")
    for note in ("The secret plan", "A different secret"):
        await client.patch(url, headers=person.headers, json={"internal_note": note})
    updates = await audit_entries(org["id"], "time_entry.updated")
    assert [u["metadata"]["changes"]["internal_note"] for u in updates] == [
        [False, True],
        [True, True],
    ]
    everything = json.dumps(await audit_entries(org["id"]))
    assert "secret" not in everything


async def test_entries_carry_the_service_of_their_job(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    acme = await make_client(org)
    accounts = await service_id(org, "ACCS")
    with_service = await make_job(org, acme, service_id=accounts)
    without_service = await make_job(org, acme)
    entry = await log_time(client, person, org, job_id=with_service.id)
    assert entry["service_id"] == str(accounts)
    assert entry["service_name"] == "Year-end accounts"
    plain = await log_time(client, person, org, job_id=without_service.id)
    assert plain["service_id"] is None and plain["service_name"] is None
    no_job = await log_time(client, person, org)
    assert no_job["service_id"] is None
    # Read-only: the entry has no service of its own.
    response = await client.patch(
        org_url(org, f"/time-entries/{entry['id']}"),
        headers=person.headers,
        json={"service_id": str(accounts)},
    )
    assert err(response, 422)
    # It is on every read of an entry.
    sheet = ok_data(
        await client.get(
            org_url(org, f"/timesheets/{entry['timesheet_id']}"), headers=person.headers
        )
    )
    assert {e["service_name"] for e in sheet["entries"]} == {"Year-end accounts", None}
