"""The practice's time increment rounds every way of recording time up in the same way."""

from httpx import AsyncClient

from tests.domain_factories import make_client, practice, staff
from tests.time_gap_helpers import approved_entry, copy_entry, copy_week, correct, day
from tests.time_helpers import age_timer, log_time, ok_data, org_url, set_practice


async def test_every_way_of_recording_time_rounds_up_to_the_increment(client: AsyncClient) -> None:
    _, org = await practice(client)
    person = await staff(org)
    approver = await staff(org, "manager")
    acme = await make_client(org)
    # Recorded to the minute, then the practice moves to a 15 minute increment.
    await set_practice(org, time_increment_minutes=1)
    odd = await log_time(client, person, org, entry_date=day(0), minutes=7)
    long = await log_time(client, person, org, entry_date=day(1), minutes=16)
    approved = await approved_entry(
        client, org, person, approver, client_id=acme.id, entry_date=day(14), minutes=61
    )
    assert (odd["minutes"], long["minutes"], approved["minutes"]) == (7, 16, 61)
    await set_practice(org, time_increment_minutes=15)

    # A new entry, a bulk create and an edit.
    fresh = await log_time(client, person, org, entry_date=day(2), minutes=1)
    assert fresh["minutes"] == 15
    bulk = ok_data(
        await client.post(
            org_url(org, "/time-entries/bulk"),
            headers=person.headers,
            json={"entries": [{"entry_date": day(3), "minutes": m} for m in (7, 16, 30)]},
        ),
        201,
    )
    assert [e["minutes"] for e in bulk] == [15, 30, 30]
    url = org_url(org, f"/time-entries/{fresh['id']}")
    edited = ok_data(await client.patch(url, headers=person.headers, json={"minutes": 16}))
    assert edited["minutes"] == 30

    # The timer: a seven minute run is stopped as one increment.
    await client.post(org_url(org, "/timer/start"), headers=person.headers, json={})
    await age_timer(org, person, 7 * 60)
    stopped = ok_data(await client.post(org_url(org, "/timer/stop"), headers=person.headers), 201)
    assert stopped["minutes"] == 15

    # Copies are new entries: a week copy and a single copy round up too.
    week = await copy_week(
        client, person, org, from_week_start=day(0), to_week_start=day(21),
        entry_ids=[odd["id"], long["id"]],
    )  # fmt: skip
    assert week["skipped"] == [] and [e["minutes"] for e in week["created"]] == [15, 30]
    single = await copy_entry(client, person, org, odd["id"], date=day(4))
    assert single["minutes"] == 15

    # A correction of approved time rounds up as well: 62 minutes become 75.
    fixed = await correct(client, approver, org, approved["id"], reason="Re-keyed", minutes=62)
    assert fixed["minutes"] == 75
