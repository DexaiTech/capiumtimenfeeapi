"""Central events (renamed, archived, restored), the change feed and the integration log."""

import asyncio

import pytest

from app.services import central_events
from tests.central_helpers import (
    ASHBY,
    BAKERY,
    JOINERY,
    KESTREL,
    PLUMBING,
    client_row,
    enabled,
    new_team,
    process_changes,
    scalar,
    send_event,
    sql,
    sync_logs,
)
from tests.clients_helpers import create_client, org_url
from tests.factories import audit_entries, create_user_with_org

# --- One event --------------------------------------------------------------------------------


async def test_a_rename_event_updates_the_linked_client_and_the_cache(client):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    response = await send_event(
        client,
        team.owner,
        team.org,
        "renamed",
        BAKERY,
        event_id="evt-1",
        name="Northgate Bakehouse Ltd",
        city="York",
        postcode="YO1 7HH",
        address_line2="Second floor",
    )
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Event processed"
    data = response.json()["data"]
    assert (data["event_id"], data["type"], data["central_id"]) == ("evt-1", "renamed", BAKERY)
    assert (data["outcome"], data["duplicate"], data["client_id"]) == (
        "applied",
        False,
        created["id"],
    )
    assert data["message"] == "Name and address updated."
    row = await client_row(created["id"])
    assert (row["name"], row["city"], row["postcode"], row["address_line2"]) == (
        "Northgate Bakehouse Ltd",
        "York",
        "YO1 7HH",
        "Second floor",
    )
    assert row["address_line1"] == "12 Northgate Street"  # not in the event: left alone
    assert (row["central_sync_state"], row["country"]) == ("synced", "GB")
    cache = (
        await sql(
            "SELECT name, city FROM central_client_cache WHERE organization_id = :o AND central_id = :c",
            o=team.id,
            c=BAKERY,
        )
    )[0]
    assert cache == {"name": "Northgate Bakehouse Ltd", "city": "York"}
    # Handlers run as the system: no actor, but the caller and the source are kept.
    [entry] = await audit_entries(team.id, "client.central_renamed")
    assert entry["actor_user_id"] is None and entry["target_id"] == created["id"]
    assert entry["metadata"]["changes"]["name"] == [
        "Northgate Bakery Ltd",
        "Northgate Bakehouse Ltd",
    ]
    assert entry["metadata"]["event_id"] == "evt-1" and entry["metadata"]["source"] == "webhook"
    assert entry["metadata"]["requested_by"] == str(team.owner.id)
    assert entry["metadata"]["central_id"] == BAKERY
    [log, _enable] = await sync_logs(client, team.owner, team.org, entity_id=created["id"])
    assert (log["operation"], log["status"], log["external_event_id"]) == ("renamed", "ok", "evt-1")
    assert log["external_id"] == BAKERY and log["details"]["source"] == "webhook"


async def test_a_rename_can_clear_an_address_line_and_confirms_a_stale_client(
    client, central_gateway
):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, PLUMBING))["client"]
    await sql("UPDATE clients SET central_sync_state = 'stale' WHERE id = :i", i=created["id"])
    response = await send_event(client, team.owner, team.org, "renamed", PLUMBING, address_line2="")
    assert response.json()["data"]["outcome"] == "applied"
    row = await client_row(created["id"])
    assert row["address_line2"] is None and row["central_sync_state"] == "synced"
    # Saying what is already true still confirms the client but changes nothing.
    again = await send_event(
        client, team.owner, team.org, "renamed", PLUMBING, name="Brightwater Plumbing & Heating Ltd"
    )
    assert again.json()["data"]["outcome"] == "skipped"
    assert again.json()["data"]["message"] == "Already up to date."


async def test_a_repeated_event_changes_nothing(client):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    first = await send_event(
        client, team.owner, team.org, "renamed", BAKERY, event_id="evt-r", name="First Name Ltd"
    )
    assert first.json()["data"]["outcome"] == "applied"
    # Delivered again (even with other content): logged as skipped, nothing changes.
    again = await send_event(
        client, team.owner, team.org, "renamed", BAKERY, event_id="evt-r", name="Second Name Ltd"
    )
    assert again.status_code == 200, again.text
    data = again.json()["data"]
    assert (data["outcome"], data["duplicate"], data["client_id"]) == ("skipped", True, None)
    assert data["message"] == "Event evt-r was already processed: nothing changed."
    assert (await client_row(created["id"]))["name"] == "First Name Ltd"
    assert len(await audit_entries(team.id, "client.central_renamed")) == 1
    logs = await sync_logs(client, team.owner, team.org, integration="central_client")
    renamed = [row for row in logs if row["operation"] == "renamed"]
    assert [(r["status"], r["external_event_id"]) for r in renamed] == [
        ("skipped", None),
        ("ok", "evt-r"),
    ]
    assert renamed[0]["details"]["duplicate"] is True


async def test_the_same_event_delivered_in_parallel_is_applied_once(client):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    responses = await asyncio.gather(
        *(
            send_event(
                client,
                team.owner,
                team.org,
                "renamed",
                BAKERY,
                event_id="evt-p",
                name="Parallel Ltd",
            )
            for _ in range(6)
        )
    )
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    outcomes = sorted(
        (r.json()["data"]["outcome"], r.json()["data"]["duplicate"]) for r in responses
    )
    assert outcomes == [("applied", False)] + [("skipped", True)] * 5
    assert (await client_row(created["id"]))["name"] == "Parallel Ltd"
    assert len(await audit_entries(team.id, "client.central_renamed")) == 1
    carrying = await scalar(
        "SELECT count(*) FROM integration_sync_logs WHERE organization_id = :o "
        "AND external_event_id = 'evt-p'",
        o=team.id,
    )
    assert carrying == 1


async def test_an_event_for_a_client_that_is_not_enabled_is_skipped_not_an_error(client):
    team = await new_team(client)
    await create_client(client, team.owner, team.org, "Local")
    for type_, extra in (
        ("renamed", {"name": "Nobody Ltd"}),
        ("archived", {}),
        ("restored", {}),
        ("merged", {"into_central_id": ASHBY}),
    ):
        response = await send_event(client, team.owner, team.org, type_, KESTREL, **extra)
        assert response.status_code == 200, (type_, response.text)
        data = response.json()["data"]
        assert (data["outcome"], data["duplicate"], data["client_id"]) == (
            "skipped",
            False,
            None,
        ), type_
    # Even an id the central service has never heard of.
    unknown = await send_event(client, team.owner, team.org, "archived", "CC-9999")
    assert unknown.status_code == 200 and unknown.json()["data"]["outcome"] == "skipped"
    logs = await sync_logs(client, team.owner, team.org, status="skipped")
    assert len(logs) == 5 and all(r["external_event_id"] for r in logs)
    assert await audit_entries(team.id, "client.central_renamed") == []


async def test_archive_and_restore_events_change_the_clients_status(client):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    archived = await send_event(client, team.owner, team.org, "archived", BAKERY, event_id="e-a")
    assert archived.json()["data"]["outcome"] == "applied"
    assert archived.json()["data"]["message"] == "Client archived."
    assert (await client_row(created["id"]))["status"] == "archived"
    assert (
        await scalar(
            "SELECT status FROM central_client_cache WHERE organization_id = :o AND central_id = :c",
            o=team.id,
            c=BAKERY,
        )
        == "archived"
    )
    [entry] = await audit_entries(team.id, "client.central_archived")
    assert entry["actor_user_id"] is None and entry["metadata"]["previous_status"] == "active"
    # Archived already: nothing to do.
    again = await send_event(client, team.owner, team.org, "archived", BAKERY)
    assert again.json()["data"]["outcome"] == "skipped"
    assert again.json()["data"]["message"] == "The client is already archived."

    restored = await send_event(client, team.owner, team.org, "restored", BAKERY, event_id="e-r")
    assert restored.json()["data"]["outcome"] == "applied"
    assert restored.json()["data"]["message"] == "Client restored."
    assert (await client_row(created["id"]))["status"] == "active"
    assert (
        await scalar(
            "SELECT status FROM central_client_cache WHERE organization_id = :o AND central_id = :c",
            o=team.id,
            c=BAKERY,
        )
        == "active"
    )
    [restored_entry] = await audit_entries(team.id, "client.central_restored")
    assert restored_entry["metadata"]["previous_status"] == "archived"
    not_archived = await send_event(client, team.owner, team.org, "restored", BAKERY)
    assert not_archived.json()["data"]["message"] == "The client is not archived."
    # Every event of the client is in its log, newest first.
    assert [
        r["operation"]
        for r in await sync_logs(client, team.owner, team.org, entity_id=created["id"])
    ][:2] == [
        "restored",
        "restored",
    ]


async def test_a_restore_event_also_restores_a_client_archived_by_hand(client):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    done = await client.post(
        org_url(team.org, f"/clients/{created['id']}/archive"), headers=team.owner.headers
    )
    assert done.status_code == 200
    restored = await send_event(client, team.owner, team.org, "restored", BAKERY)
    assert restored.json()["data"]["outcome"] == "applied"
    assert (await client_row(created["id"]))["status"] == "active"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"type": "renamed", "central_id": BAKERY, "name": "x"},  # no event id
        {"event_id": "", "type": "archived", "central_id": BAKERY},
        {"event_id": "e" * 101, "type": "archived", "central_id": BAKERY},
        {"event_id": "e1", "type": "teleported", "central_id": BAKERY},
        {"event_id": "e1", "type": "archived", "central_id": "bad id"},
        {"event_id": "e1", "type": "archived"},
        {"event_id": "e1", "type": "renamed", "central_id": BAKERY},
        {"event_id": "e1", "type": "renamed", "central_id": BAKERY, "name": ""},
        {"event_id": "e1", "type": "renamed", "central_id": BAKERY, "name": "x" * 201},
        {
            "event_id": "e1",
            "type": "renamed",
            "central_id": BAKERY,
            "name": "x",
            "country": "Britain",
        },
        {"event_id": "e1", "type": "merged", "central_id": BAKERY},
        {"event_id": "e1", "type": "merged", "central_id": BAKERY, "into_central_id": BAKERY},
        {"event_id": "e1", "type": "archived", "central_id": BAKERY, "into_central_id": ASHBY},
        {"event_id": "e1", "type": "archived", "central_id": BAKERY, "name": "ignored", "x": 1},
        {"event_id": "e1", "type": "archived", "central_id": BAKERY, "occurred_at": "yesterday"},
    ],
)
async def test_an_event_is_validated(client, body):
    team = await new_team(client)
    await enabled(client, team.owner, team.org, BAKERY)
    response = await client.post(
        org_url(team.org, "/central-clients/events"), headers=team.owner.headers, json=body
    )
    assert response.status_code == 422, response.text
    assert (
        await scalar("SELECT count(*) FROM integration_sync_logs WHERE operation <> 'enable'") == 0
    )


async def test_a_lower_case_country_is_accepted_and_stored_upper_case(client):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    response = await send_event(
        client, team.owner, team.org, "renamed", BAKERY, country="ie", city="Dublin"
    )
    assert response.json()["data"]["outcome"] == "applied"
    row = await client_row(created["id"])
    assert (row["country"], row["city"]) == ("IE", "Dublin")


async def test_events_need_settings_manage_and_a_practice_you_belong_to(client):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    for role in ("staff", "manager", "viewer", "billing"):
        member = await team.member(role)
        response = await send_event(client, member, team.org, "archived", BAKERY)
        assert response.status_code == 403, role
    assert (await client_row(created["id"]))["status"] == "active"
    for role in ("admin", "partner"):
        member = await team.member(role)
        response = await send_event(
            client, member, team.org, "renamed", BAKERY, name=f"By {role} Ltd"
        )
        assert response.status_code == 200, role
    outsider, _ = await create_user_with_org(client, "Elsewhere")
    assert (await send_event(client, outsider, team.org, "archived", BAKERY)).status_code == 404
    anonymous = await client.post(
        org_url(team.org, "/central-clients/events"),
        json={"event_id": "e", "type": "archived", "central_id": BAKERY},
    )
    assert anonymous.status_code == 401


async def test_events_only_touch_the_practice_they_are_sent_to(client):
    first = await new_team(client)
    owner, org = await create_user_with_org(client, "Second Practice")
    mine = (await enabled(client, first.owner, first.org, BAKERY))["client"]
    theirs = (await enabled(client, owner, org, BAKERY))["client"]
    response = await send_event(
        client, first.owner, first.org, "renamed", BAKERY, name="Mine Only Ltd"
    )
    assert response.json()["data"]["client_id"] == mine["id"]
    assert (await client_row(mine["id"]))["name"] == "Mine Only Ltd"
    assert (await client_row(theirs["id"]))["name"] == "Northgate Bakery Ltd"
    # The same event id in the other practice is a different delivery.
    other = await send_event(
        client,
        owner,
        org,
        "renamed",
        BAKERY,
        event_id=response.json()["data"]["event_id"],
        name="Theirs Ltd",
    )
    assert (
        other.json()["data"]["outcome"] == "applied" and other.json()["data"]["duplicate"] is False
    )


async def test_a_failing_event_is_logged_and_can_be_delivered_again(client, monkeypatch):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]

    async def boom(self, *args, **kwargs):
        raise RuntimeError("database on fire")

    with monkeypatch.context() as patch:
        patch.setattr(central_events.CentralEventService, "_renamed", boom)
        failed = await send_event(
            client,
            team.owner,
            team.org,
            "renamed",
            BAKERY,
            event_id="evt-f",
            name="Never Saved Ltd",
        )
    assert failed.status_code == 500
    assert "on fire" not in failed.text  # nothing internal leaks
    assert (await client_row(created["id"]))["name"] == "Northgate Bakery Ltd"
    [log] = await sync_logs(client, team.owner, team.org, status="failed")
    assert (log["operation"], log["external_id"], log["attempt"]) == ("renamed", BAKERY, 1)
    assert log["external_event_id"] is None and "RuntimeError" in log["message"]
    assert log["details"] == {"event_id": "evt-f", "source": "webhook"}
    # A failed delivery is not "processed": the retry goes through.
    retry = await send_event(
        client, team.owner, team.org, "renamed", BAKERY, event_id="evt-f", name="Saved Ltd"
    )
    assert retry.status_code == 200 and retry.json()["data"]["outcome"] == "applied"
    assert (await client_row(created["id"]))["name"] == "Saved Ltd"


# --- The change feed -----------------------------------------------------------------------------


async def test_process_changes_applies_the_central_feed_through_the_same_handler(
    client, central_gateway
):
    team = await new_team(client)
    bakery = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    plumbing = (await enabled(client, team.owner, team.org, PLUMBING))["client"]
    central_gateway.rename(BAKERY, name="Northgate Bakehouse Ltd", city="York")
    central_gateway.archive(PLUMBING)
    central_gateway.rename(KESTREL, name="Kestrel Digital Ltd")  # not enabled here
    response = await process_changes(client, team.owner, team.org)
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Central changes processed"
    data = response.json()["data"]
    assert {
        k: data[k]
        for k in ("available", "fetched", "applied", "skipped", "failed", "has_more", "cursor")
    } == {
        "available": True,
        "fetched": 3,
        "applied": 2,
        "skipped": 1,
        "failed": 0,
        "has_more": False,
        "cursor": "3",
    }
    assert [(e["event_id"], e["type"], e["outcome"]) for e in data["events"]] == [
        ("mock-evt-000001", "renamed", "applied"),
        ("mock-evt-000002", "archived", "applied"),
        ("mock-evt-000003", "renamed", "skipped"),
    ]
    assert (await client_row(bakery["id"]))["name"] == "Northgate Bakehouse Ltd"
    assert (await client_row(bakery["id"]))["city"] == "York"
    assert (await client_row(plumbing["id"]))["status"] == "archived"
    [entry] = await audit_entries(team.id, "client.central_renamed")
    assert entry["metadata"]["source"] == "feed" and entry["actor_user_id"] is None
    poll = [r for r in await sync_logs(client, team.owner, team.org) if r["operation"] == "poll"]
    assert [(p["status"], p["message"]) for p in poll] == [
        ("ok", "Read 3 event(s): 2 applied, 1 skipped.")
    ]

    # Nothing new: nothing read. New events: only those.
    quiet = (await process_changes(client, team.owner, team.org)).json()["data"]
    assert (quiet["fetched"], quiet["cursor"], quiet["events"]) == (0, "3", [])
    central_gateway.restore(PLUMBING)
    more = (await process_changes(client, team.owner, team.org)).json()["data"]
    assert (more["fetched"], more["applied"], more["cursor"]) == (1, 1, "4")
    assert (await client_row(plumbing["id"]))["status"] == "active"


async def test_a_replayed_feed_is_recognised_event_by_event(client, central_gateway):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    central_gateway.rename(BAKERY, name="Once Only Ltd")
    central_gateway.archive(BAKERY)
    first = (await process_changes(client, team.owner, team.org)).json()["data"]
    assert (first["applied"], first["skipped"]) == (2, 0)
    # The cursor is lost (say, a restored backup): the feed is read again from the start.
    await sql("DELETE FROM integration_cursors WHERE organization_id = :o", o=team.id)
    replay = (await process_changes(client, team.owner, team.org)).json()["data"]
    assert (replay["fetched"], replay["applied"], replay["skipped"], replay["cursor"]) == (
        2,
        0,
        2,
        "2",
    )
    assert all(e["duplicate"] for e in replay["events"])
    assert len(await audit_entries(team.id, "client.central_renamed")) == 1
    assert (await client_row(created["id"]))["status"] == "archived"


async def test_process_changes_reads_the_feed_in_batches(client, central_gateway, monkeypatch):
    monkeypatch.setattr(central_events, "CHANGE_BATCH", 2)
    team = await new_team(client)
    await enabled(client, team.owner, team.org, BAKERY)
    for n in range(3):
        central_gateway.rename(BAKERY, city=f"Town {n}")
    first = (await process_changes(client, team.owner, team.org)).json()["data"]
    assert (first["fetched"], first["has_more"], first["cursor"]) == (2, True, "2")
    second = (await process_changes(client, team.owner, team.org)).json()["data"]
    assert (second["fetched"], second["has_more"], second["cursor"]) == (1, False, "3")
    assert (await sql("SELECT city FROM clients WHERE organization_id = :o", o=team.id))[0][
        "city"
    ] == "Town 2"


async def test_every_practice_has_its_own_cursor(client, central_gateway):
    first = await new_team(client)
    owner, org = await create_user_with_org(client, "Second Practice")
    one = (await enabled(client, first.owner, first.org, BAKERY))["client"]
    two = (await enabled(client, owner, org, BAKERY))["client"]
    central_gateway.rename(BAKERY, name="Shared Rename Ltd")
    mine = (await process_changes(client, first.owner, first.org)).json()["data"]
    assert (mine["fetched"], mine["applied"]) == (1, 1)
    assert (await client_row(two["id"]))["name"] == "Northgate Bakery Ltd"  # not read there yet
    theirs = (await process_changes(client, owner, org)).json()["data"]
    assert (theirs["fetched"], theirs["applied"]) == (1, 1)
    assert (
        (await client_row(one["id"]))["name"]
        == (await client_row(two["id"]))["name"]
        == "Shared Rename Ltd"
    )


async def test_process_changes_while_central_is_down(client, central_gateway):
    team = await new_team(client)
    await enabled(client, team.owner, team.org, BAKERY)
    central_gateway.rename(BAKERY, city="York")
    central_gateway.set_available(False)
    response = await process_changes(client, team.owner, team.org)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["available"], data["fetched"], data["events"], data["cursor"]) == (
        False,
        0,
        [],
        None,
    )
    [log] = await sync_logs(client, team.owner, team.org, status="failed")
    assert (log["operation"], log["message"]) == (
        "poll",
        "The central client service is unavailable.",
    )
    central_gateway.set_available(True)
    back = (await process_changes(client, team.owner, team.org)).json()["data"]
    assert (back["available"], back["applied"]) == (True, 1)


async def test_a_failure_stops_the_batch_and_keeps_the_cursor(client, central_gateway, monkeypatch):
    team = await new_team(client)
    bakery = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    plumbing = (await enabled(client, team.owner, team.org, PLUMBING))["client"]
    joinery = (await enabled(client, team.owner, team.org, JOINERY))["client"]
    central_gateway.rename(BAKERY, city="York")
    central_gateway.archive(PLUMBING)
    central_gateway.rename(JOINERY, city="Bradford")

    async def boom(self, *args, **kwargs):
        raise RuntimeError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(central_events.CentralEventService, "_archived", boom)
        broken = (await process_changes(client, team.owner, team.org)).json()["data"]
    assert (broken["fetched"], broken["applied"], broken["failed"], broken["has_more"]) == (
        3,
        1,
        1,
        True,
    )
    assert broken["cursor"] is None
    assert [e["event_id"] for e in broken["events"]] == ["mock-evt-000001"]
    assert (await client_row(bakery["id"]))["city"] == "York"
    assert (await client_row(plumbing["id"]))["status"] == "active"
    assert (await client_row(joinery["id"]))["city"] == "Halifax"  # the batch stopped before it
    poll = [r for r in await sync_logs(client, team.owner, team.org) if r["operation"] == "poll"]
    assert [(p["status"], p["message"]) for p in poll] == [
        ("failed", "Read 3 event(s): 1 applied, 0 skipped, then one failed.")
    ]
    failures = await sync_logs(client, team.owner, team.org, status="failed")
    assert sorted(r["operation"] for r in failures) == ["archived", "poll"]

    # The next run reads the batch again: what went through is skipped as a repeat.
    fixed = (await process_changes(client, team.owner, team.org)).json()["data"]
    assert (fixed["fetched"], fixed["applied"], fixed["skipped"], fixed["failed"]) == (3, 2, 1, 0)
    assert fixed["cursor"] == "3" and fixed["has_more"] is False
    assert [(e["event_id"], e["duplicate"]) for e in fixed["events"]] == [
        ("mock-evt-000001", True),
        ("mock-evt-000002", False),
        ("mock-evt-000003", False),
    ]
    assert (await client_row(plumbing["id"]))["status"] == "archived"
    assert (await client_row(joinery["id"]))["city"] == "Bradford"


async def test_process_changes_needs_settings_manage(client, central_gateway):
    team = await new_team(client)
    for role in ("staff", "manager", "viewer", "billing"):
        member = await team.member(role)
        assert (await process_changes(client, member, team.org)).status_code == 403, role
    assert (await process_changes(client, await team.member("admin"), team.org)).status_code == 200
    outsider, _ = await create_user_with_org(client, "Elsewhere")
    assert (await process_changes(client, outsider, team.org)).status_code == 404


# --- The integration log -----------------------------------------------------------------------------


async def test_the_sync_log_lists_every_attempt_newest_first_with_filters(client, central_gateway):
    team = await new_team(client)
    first = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    second = (await enabled(client, team.owner, team.org, PLUMBING))["client"]
    central_gateway.rename(BAKERY, city="York")
    central_gateway.set_available(False)
    await client.post(
        org_url(team.org, f"/clients/{first['id']}/central-sync"), headers=team.owner.headers
    )
    central_gateway.set_available(True)
    await send_event(client, team.owner, team.org, "archived", KESTREL)  # skipped

    everything = await sync_logs(client, team.owner, team.org)
    assert [(r["operation"], r["status"]) for r in everything] == [
        ("archived", "skipped"),
        ("sync", "failed"),
        ("enable", "ok"),
        ("enable", "ok"),
    ]
    row = everything[1]
    assert set(row) == {
        "id",
        "integration",
        "direction",
        "operation",
        "entity_type",
        "entity_id",
        "external_id",
        "external_event_id",
        "status",
        "message",
        "attempt",
        "details",
        "created_at",
    }
    assert (row["integration"], row["direction"], row["entity_type"], row["entity_id"]) == (
        "central_client",
        "inbound",
        "client",
        first["id"],
    )
    assert [
        r["entity_id"]
        for r in await sync_logs(client, team.owner, team.org, entity_id=second["id"])
    ] == [second["id"]]
    assert [
        r["status"] for r in await sync_logs(client, team.owner, team.org, status="failed")
    ] == ["failed"]
    assert len(await sync_logs(client, team.owner, team.org, integration="central_client")) == 4
    assert await sync_logs(client, team.owner, team.org, integration="practice_management") == []
    assert await sync_logs(client, team.owner, team.org, status="ok", entity_id=first["id"]) == [
        r for r in everything if r["entity_id"] == first["id"] and r["status"] == "ok"
    ]


async def test_the_sync_log_is_paginated_by_cursor(client):
    team = await new_team(client)
    ids = []
    for central_id in (BAKERY, PLUMBING, JOINERY, ASHBY, KESTREL):
        ids.append((await enabled(client, team.owner, team.org, central_id))["client"]["id"])
    url = org_url(team.org, "/integrations/sync-logs")
    page1 = await client.get(url, headers=team.owner.headers, params={"limit": 2})
    assert page1.status_code == 200, page1.text
    assert [r["entity_id"] for r in page1.json()["data"]] == [ids[4], ids[3]]
    assert page1.json()["meta"]["has_more"] is True
    page2 = await client.get(
        url,
        headers=team.owner.headers,
        params={"limit": 2, "cursor": page1.json()["meta"]["next_cursor"]},
    )
    assert [r["entity_id"] for r in page2.json()["data"]] == [ids[2], ids[1]]
    page3 = await client.get(
        url,
        headers=team.owner.headers,
        params={"limit": 2, "cursor": page2.json()["meta"]["next_cursor"]},
    )
    assert [r["entity_id"] for r in page3.json()["data"]] == [ids[0]]
    assert page3.json()["meta"]["has_more"] is False


@pytest.mark.parametrize(
    "params",
    [
        {"status": "nope"},
        {"integration": "Bad Name"},
        {"integration": "x"},
        {"entity_id": "not-a-uuid"},
        {"limit": 0},
        {"cursor": "garbage"},
    ],
)
async def test_the_sync_log_validates_its_filters(client, params):
    team = await new_team(client)
    response = await client.get(
        org_url(team.org, "/integrations/sync-logs"), headers=team.owner.headers, params=params
    )
    assert response.status_code == 422, response.text


async def test_the_sync_log_needs_settings_manage_and_is_private_to_the_practice(client):
    team = await new_team(client)
    await enabled(client, team.owner, team.org, BAKERY)
    for role in ("staff", "manager", "viewer", "billing"):
        member = await team.member(role)
        denied = await client.get(
            org_url(team.org, "/integrations/sync-logs"), headers=member.headers
        )
        assert denied.status_code == 403, role
    owner, org = await create_user_with_org(client, "Second Practice")
    assert await sync_logs(client, owner, org) == []
    outsider_view = await client.get(
        org_url(team.org, "/integrations/sync-logs"), headers=owner.headers
    )
    assert outsider_view.status_code == 404
    admin = await team.member("admin")
    assert len(await sync_logs(client, admin, team.org)) == 1


async def test_the_log_survives_deleting_the_client_it_is_about(client):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    gone = await client.delete(
        org_url(team.org, f"/clients/{created['id']}"), headers=team.owner.headers
    )
    assert gone.status_code in (200, 204), gone.text
    [log] = await sync_logs(client, team.owner, team.org)
    assert log["entity_id"] == created["id"]
