"""Linking clients to the central service, the centrally managed fields, and synchronisation."""

import pytest

from app.services import central_sync
from tests.central_helpers import (
    ARCHIVED,
    BAKERY,
    JOINERY,
    PLUMBING,
    client_row,
    enable,
    enabled,
    link,
    new_team,
    scalar,
    search,
    sync_all,
    sync_client,
    sync_logs,
    unlink,
)
from tests.clients_helpers import create_client, org_url
from tests.factories import audit_entries, create_user_with_org, error

MANAGED = {
    "name": "A Better Name Ltd",
    "address_line1": "1 New Street",
    "address_line2": "Floor 2",
    "city": "York",
    "postcode": "YO1 1AA",
    "country": "FR",
}


def client_url(team, client_id: str, tail: str = "") -> str:
    return org_url(team.org, f"/clients/{client_id}{tail}")


# --- Link -------------------------------------------------------------------------------


async def test_link_gives_a_local_client_the_central_name_and_address(client):
    team = await new_team(client)
    local = await create_client(
        client,
        team.owner,
        team.org,
        "North Gate Bakery",
        email="hello@northgate.example",
        address_line1="1 Old Road",
        city="Hull",
        segment="Gold",
    )
    assert (local["central_client_id"], local["central_sync_state"]) == (None, "local")
    response = await link(client, team.owner, team.org, local["id"], BAKERY)
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Client linked"
    linked = response.json()["data"]
    assert (linked["name"], linked["address_line1"], linked["city"]) == (
        "Northgate Bakery Ltd",
        "12 Northgate Street",
        "Leeds",
    )
    assert (linked["postcode"], linked["country"]) == ("LS1 4AB", "GB")
    # The company number was missing, so it is taken; everything else stays.
    assert linked["company_number"] == "09231457"
    assert (linked["email"], linked["segment"]) == ("hello@northgate.example", "Gold")
    assert (linked["central_client_id"], linked["central_sync_state"]) == (BAKERY, "synced")
    assert linked["central_synced_at"] is not None

    [entry] = await audit_entries(team.id, "client.central_linked")
    assert entry["actor_user_id"] == str(team.owner.id) and entry["target_id"] == local["id"]
    assert entry["metadata"]["central_id"] == BAKERY
    assert entry["metadata"]["changes"]["name"] == ["North Gate Bakery", "Northgate Bakery Ltd"]
    assert entry["metadata"]["changes"]["city"] == ["Hull", "Leeds"]
    [log] = await sync_logs(client, team.owner, team.org, entity_id=local["id"])
    assert (log["operation"], log["status"], log["external_id"]) == ("link", "ok", BAKERY)
    assert (
        await scalar(
            "SELECT count(*) FROM central_client_cache WHERE organization_id = :o", o=team.id
        )
        == 1
    )


async def test_link_keeps_the_company_number_the_client_already_has(client):
    team = await new_team(client)
    local = await create_client(client, team.owner, team.org, "Bakery", company_number="12345678")
    linked = (await link(client, team.owner, team.org, local["id"], BAKERY)).json()["data"]
    assert linked["company_number"] == "12345678"


async def test_a_central_client_cannot_be_linked_to_two_clients(client):
    team = await new_team(client)
    first = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    other = await create_client(client, team.owner, team.org, "Another Bakery")
    refused = await link(client, team.owner, team.org, other["id"], BAKERY)
    assert refused.status_code == 409, refused.text
    assert error(refused)["code"] == "CENTRAL_CLIENT_ALREADY_LINKED"
    assert error(refused)["details"] == {"client_id": first["id"], "client_code": first["code"]}
    assert (await client_row(other["id"]))["central_client_id"] is None


async def test_a_linked_client_must_be_unlinked_before_it_is_linked_again(client):
    team = await new_team(client)
    first = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    refused = await link(client, team.owner, team.org, first["id"], PLUMBING)
    assert refused.status_code == 409
    assert error(refused)["code"] == "CLIENT_ALREADY_LINKED"
    assert error(refused)["details"] == {"central_id": BAKERY}
    same = await link(client, team.owner, team.org, first["id"], BAKERY)
    assert same.status_code == 409 and error(same)["code"] == "CLIENT_ALREADY_LINKED"


async def test_link_refuses_unknown_archived_and_malformed_ids(client):
    team = await new_team(client)
    local = await create_client(client, team.owner, team.org, "Local")
    unknown = await link(client, team.owner, team.org, local["id"], "CC-9999")
    assert unknown.status_code == 404
    archived = await link(client, team.owner, team.org, local["id"], ARCHIVED)
    assert archived.status_code == 409 and error(archived)["code"] == "CENTRAL_CLIENT_ARCHIVED"
    missing = await link(
        client, team.owner, team.org, "00000000-0000-4000-8000-000000000000", BAKERY
    )
    assert missing.status_code == 404
    for body in ({}, {"central_id": ""}, {"central_id": "bad id"}, {"central_id": BAKERY, "x": 1}):
        response = await client.post(
            client_url(team, local["id"], "/central-link"), headers=team.owner.headers, json=body
        )
        assert response.status_code == 422, body
    assert (await client_row(local["id"]))["central_client_id"] is None


async def test_link_uses_the_cache_while_central_is_down(client, central_gateway):
    team = await new_team(client)
    local = await create_client(client, team.owner, team.org, "Local")
    central_gateway.set_available(False)
    uncached = await link(client, team.owner, team.org, local["id"], BAKERY)
    assert uncached.status_code == 503 and error(uncached)["code"] == "CENTRAL_UNAVAILABLE"
    central_gateway.set_available(True)
    await search(client, team.owner, team.org, q="northgate")
    central_gateway.set_available(False)
    cached = await link(client, team.owner, team.org, local["id"], BAKERY)
    assert cached.status_code == 200, cached.text
    data = cached.json()["data"]
    assert (data["name"], data["central_sync_state"]) == ("Northgate Bakery Ltd", "stale")
    [entry] = await audit_entries(team.id, "client.central_linked")
    assert entry["metadata"]["from_cache"] is True


async def test_link_and_unlink_need_client_create_and_a_client_of_the_practice(client):
    team = await new_team(client)
    local = await create_client(client, team.owner, team.org, "Local")
    for role in ("staff", "viewer", "billing"):
        member = await team.member(role)
        assert (await link(client, member, team.org, local["id"], BAKERY)).status_code == 403, role
        assert (await unlink(client, member, team.org, local["id"])).status_code == 403, role
    manager = await team.member("manager")
    assert (await link(client, manager, team.org, local["id"], BAKERY)).status_code == 200
    assert (await unlink(client, manager, team.org, local["id"])).status_code == 200
    # Another practice's client is not found, whoever asks.
    owner, org = await create_user_with_org(client, "Other Practice")
    theirs = await create_client(client, owner, org, "Theirs")
    assert (await link(client, team.owner, team.org, theirs["id"], BAKERY)).status_code == 404
    assert (await unlink(client, team.owner, team.org, theirs["id"])).status_code == 404
    assert (await link(client, owner, team.org, local["id"], BAKERY)).status_code == 404


# --- Unlink -----------------------------------------------------------------------------


async def test_unlinking_makes_the_client_local_and_editable_again(client):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    response = await unlink(client, team.owner, team.org, created["id"])
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Client unlinked"
    data = response.json()["data"]
    assert (data["central_client_id"], data["central_sync_state"], data["central_synced_at"]) == (
        None,
        "local",
        None,
    )
    assert data["name"] == "Northgate Bakery Ltd"  # the values stay, they are just editable
    renamed = await client.patch(
        client_url(team, created["id"]), headers=team.owner.headers, json={"name": "Our Bakery"}
    )
    assert renamed.status_code == 200 and renamed.json()["data"]["name"] == "Our Bakery"
    [entry] = await audit_entries(team.id, "client.central_unlinked")
    assert entry["metadata"]["central_id"] == BAKERY and entry["target_id"] == created["id"]
    logs = await sync_logs(client, team.owner, team.org, entity_id=created["id"])
    assert [(row["operation"], row["status"]) for row in logs] == [
        ("unlink", "ok"),
        ("enable", "ok"),
    ]
    again = await unlink(client, team.owner, team.org, created["id"])
    assert again.status_code == 409 and error(again)["code"] == "CLIENT_NOT_LINKED"


async def test_enabling_after_unlinking_relinks_the_same_client(client):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    await unlink(client, team.owner, team.org, created["id"])
    again = await enable(client, team.owner, team.org, BAKERY)
    assert again.status_code == 200, again.text
    assert again.json()["data"]["result"] == "linked_existing"
    assert again.json()["data"]["client"]["id"] == created["id"]
    assert await scalar("SELECT count(*) FROM clients WHERE organization_id = :o", o=team.id) == 1


async def test_another_client_can_take_over_a_central_client_after_the_unlink(client):
    team = await new_team(client)
    first = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    other = await create_client(client, team.owner, team.org, "Another Bakery")
    await unlink(client, team.owner, team.org, first["id"])
    taken = await link(client, team.owner, team.org, other["id"], BAKERY)
    assert taken.status_code == 200, taken.text


# --- The centrally managed fields --------------------------------------------------------


@pytest.mark.parametrize("field", sorted(MANAGED))
async def test_a_linked_clients_name_and_address_are_managed_centrally(client, field):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    response = await client.patch(
        client_url(team, created["id"]), headers=team.owner.headers, json={field: MANAGED[field]}
    )
    assert response.status_code == 409, response.text
    failure = error(response)
    assert failure["code"] == "CLIENT_FIELD_MANAGED_CENTRALLY"
    assert [d["field"] for d in failure["details"]] == [field]
    assert failure["details"][0]["code"] == "managed_centrally"
    assert (await client_row(created["id"]))[field] == created[field]


async def test_every_managed_field_is_named_and_nothing_is_saved(client):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    response = await client.patch(
        client_url(team, created["id"]),
        headers=team.owner.headers,
        json={"name": "New", "city": "York", "email": "new@example.com", "notes": "x"},
    )
    assert response.status_code == 409
    assert [d["field"] for d in error(response)["details"]] == ["name", "city"]
    row = await client_row(created["id"])
    assert (row["name"], row["city"], row["email"], row["notes"]) == (
        "Northgate Bakery Ltd",
        "Leeds",
        None,
        None,
    )


async def test_other_fields_of_a_linked_client_stay_editable(client):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    response = await client.patch(
        client_url(team, created["id"]),
        headers=team.owner.headers,
        json={
            # Saying what it already is changes nothing, so it is allowed.
            "name": "Northgate Bakery Ltd",
            "city": "Leeds",
            "address_line2": "",
            "email": "hello@northgate.example",
            "phone": "0113 496 0000",
            "segment": "Gold",
            "invoice_email": "accounts@northgate.example",
            "notes": "Lovely people",
            "company_number": "09231457",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["email"], data["segment"], data["invoice_email"]) == (
        "hello@northgate.example",
        "Gold",
        "accounts@northgate.example",
    )
    assert data["central_client_id"] == BAKERY and data["central_sync_state"] == "synced"


async def test_the_central_fields_cannot_be_set_through_create_or_update(client):
    team = await new_team(client)
    for field, value in (
        ("central_client_id", "CC-0001"),
        ("central_sync_state", "synced"),
        ("central_synced_at", "2026-01-01T00:00:00Z"),
        ("merged_into_client_id", "00000000-0000-4000-8000-000000000000"),
    ):
        created = await client.post(
            org_url(team.org, "/clients"),
            headers=team.owner.headers,
            json={"name": "Sneaky", field: value},
        )
        assert created.status_code == 422, field
    local = await create_client(client, team.owner, team.org, "Local")
    patched = await client.patch(
        client_url(team, local["id"]),
        headers=team.owner.headers,
        json={"central_client_id": "CC-1"},
    )
    assert patched.status_code == 422


async def test_clients_report_their_central_state_and_can_be_filtered_by_it(client):
    team = await new_team(client)
    local = await create_client(client, team.owner, team.org, "Local Co")
    first = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    second = (await enabled(client, team.owner, team.org, PLUMBING))["client"]
    detail = await client.get(client_url(team, local["id"]), headers=team.owner.headers)
    data = detail.json()["data"]
    assert (data["central_client_id"], data["central_sync_state"]) == (None, "local")
    assert (data["central_synced_at"], data["merged_into_client_id"]) == (None, None)
    assert (data["merged_into"], data["default_service"], data["invoice_template"]) == (
        None,
        None,
        None,
    )

    async def names(**params) -> list[str]:
        response = await client.get(
            org_url(team.org, "/clients"), headers=team.owner.headers, params=params
        )
        assert response.status_code == 200, response.text
        return [c["name"] for c in response.json()["data"]]

    assert await names(central="linked", sort="name", order="asc") == [
        "Brightwater Plumbing & Heating Ltd",
        "Northgate Bakery Ltd",
    ]
    assert await names(central="local") == ["Local Co"]
    assert sorted(await names()) == sorted(
        ["Local Co", "Brightwater Plumbing & Heating Ltd", "Northgate Bakery Ltd"]
    )
    bad = await client.get(
        org_url(team.org, "/clients"), headers=team.owner.headers, params={"central": "maybe"}
    )
    assert bad.status_code == 422
    listed = await client.get(
        org_url(team.org, "/clients"), headers=team.owner.headers, params={"central": "linked"}
    )
    rows = {c["id"]: c for c in listed.json()["data"]}
    assert rows[first["id"]]["central_client_id"] == BAKERY
    assert rows[second["id"]]["central_client_id"] == PLUMBING
    summary = await client.get(org_url(team.org, "/clients/summary"), headers=team.owner.headers)
    assert summary.json()["data"]["by_central"] == {"linked": 2, "local": 1}
    assert summary.json()["data"]["total"] == 3
    # An archived client leaves the counts, like it leaves the default list.
    archive = await client.post(
        client_url(team, second["id"], "/archive"), headers=team.owner.headers
    )
    assert archive.status_code == 200, archive.text
    summary = await client.get(org_url(team.org, "/clients/summary"), headers=team.owner.headers)
    assert summary.json()["data"]["by_central"] == {"linked": 1, "local": 1}
    assert summary.json()["data"]["by_status"]["archived"] == 1


# --- Synchronise one client -------------------------------------------------------------------


async def test_sync_pulls_the_name_and_address_and_reports_what_changed(client, central_gateway):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    central_gateway.rename(
        BAKERY, name="Northgate Bakery & Cafe Ltd", city="York", address_line2="Floor 1"
    )
    response = await sync_client(client, team.owner, team.org, created["id"])
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Client synchronised"
    data = response.json()["data"]
    assert data["changes"] == {
        "name": ["Northgate Bakery Ltd", "Northgate Bakery & Cafe Ltd"],
        "address_line2": [None, "Floor 1"],
        "city": ["Leeds", "York"],
    }
    assert (data["changed"], data["state"], data["from_cache"]) == (True, "synced", False)
    assert (data["client_id"], data["code"], data["central_id"]) == (created["id"], "C0001", BAKERY)
    assert data["name"] == "Northgate Bakery & Cafe Ltd"
    assert data["message"] == "Name and address updated." and data["synced_at"] is not None
    row = await client_row(created["id"])
    assert (row["name"], row["city"], row["address_line2"]) == (
        "Northgate Bakery & Cafe Ltd",
        "York",
        "Floor 1",
    )
    assert row["central_sync_state"] == "synced"
    cached = await scalar(
        "SELECT name FROM central_client_cache WHERE organization_id = :o AND central_id = :c",
        o=team.id,
        c=BAKERY,
    )
    assert cached == "Northgate Bakery & Cafe Ltd"
    [entry] = await audit_entries(team.id, "client.central_synced")
    assert (
        entry["metadata"]["changes"] == data["changes"] and entry["metadata"]["state"] == "synced"
    )
    [log, _enable] = await sync_logs(client, team.owner, team.org, entity_id=created["id"])
    assert (log["operation"], log["status"], log["attempt"]) == ("sync", "ok", 1)
    assert log["details"]["changes"] == data["changes"]

    # Nothing changed centrally: nothing to report, and no new audit entry (but the attempt is logged).
    again = (await sync_client(client, team.owner, team.org, created["id"])).json()["data"]
    assert (again["changed"], again["changes"], again["message"]) == (
        False,
        {},
        "Already up to date.",
    )
    assert len(await audit_entries(team.id, "client.central_synced")) == 1
    assert len(await sync_logs(client, team.owner, team.org, entity_id=created["id"])) == 3


async def test_sync_can_clear_an_address_line(client, central_gateway):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, PLUMBING))["client"]
    assert created["address_line2"] == "Wharf Road"
    central_gateway.rename(PLUMBING, address_line2="")
    data = (await sync_client(client, team.owner, team.org, created["id"])).json()["data"]
    assert data["changes"] == {"address_line2": ["Wharf Road", None]}
    assert (await client_row(created["id"]))["address_line2"] is None


async def test_sync_while_central_is_down_keeps_the_values_and_marks_the_client_stale(
    client, central_gateway
):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    before = await client_row(created["id"])
    central_gateway.rename(BAKERY, name="Renamed While Down Ltd")
    central_gateway.set_available(False)
    first = await sync_client(client, team.owner, team.org, created["id"])
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert (data["state"], data["from_cache"], data["changed"], data["changes"]) == (
        "stale",
        True,
        False,
        {},
    )
    assert data["name"] == "Northgate Bakery Ltd"
    assert data["message"] == (
        "The central client service is unavailable: the stored name and address were kept."
    )
    row = await client_row(created["id"])
    assert row["central_sync_state"] == "stale" and row["name"] == "Northgate Bakery Ltd"
    assert row["central_synced_at"] == before["central_synced_at"]
    second = await sync_client(client, team.owner, team.org, created["id"])
    assert second.status_code == 200 and second.json()["data"]["state"] == "stale"
    logs = await sync_logs(client, team.owner, team.org, status="failed")
    assert [(row["operation"], row["attempt"]) for row in logs] == [("sync", 2), ("sync", 1)]
    assert "unavailable" in logs[0]["message"]
    assert [
        e["metadata"]["state"] for e in await audit_entries(team.id, "client.central_synced")
    ] == [
        "stale",
        "stale",
    ]
    # The central service is back: the next synchronisation catches up and the count of failures starts over.
    central_gateway.set_available(True)
    back = (await sync_client(client, team.owner, team.org, created["id"])).json()["data"]
    assert (back["state"], back["name"]) == ("synced", "Renamed While Down Ltd")
    assert (await client_row(created["id"]))["central_sync_state"] == "synced"
    central_gateway.set_available(False)
    await sync_client(client, team.owner, team.org, created["id"])
    newest = (await sync_logs(client, team.owner, team.org, status="failed"))[0]
    assert newest["attempt"] == 1


async def test_sync_marks_a_client_failed_when_central_no_longer_knows_it(
    client, central_gateway, monkeypatch
):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]

    async def nobody(central_id: str):
        return None

    monkeypatch.setattr(central_gateway, "get", nobody)
    data = (await sync_client(client, team.owner, team.org, created["id"])).json()["data"]
    assert data["state"] == "failed" and data["from_cache"] is False
    assert data["message"] == "The central service no longer knows this client."
    assert (await client_row(created["id"]))["central_sync_state"] == "failed"
    [log] = await sync_logs(client, team.owner, team.org, status="failed")
    assert log["operation"] == "sync" and log["external_id"] == BAKERY


async def test_sync_refuses_clients_that_are_not_linked_or_not_found(client):
    team = await new_team(client)
    local = await create_client(client, team.owner, team.org, "Local")
    refused = await sync_client(client, team.owner, team.org, local["id"])
    assert refused.status_code == 409 and error(refused)["code"] == "CLIENT_NOT_LINKED"
    missing = await sync_client(
        client, team.owner, team.org, "00000000-0000-4000-8000-000000000000"
    )
    assert missing.status_code == 404
    owner, org = await create_user_with_org(client, "Other Practice")
    theirs = await create_client(client, owner, org, "Theirs")
    assert (await sync_client(client, team.owner, team.org, theirs["id"])).status_code == 404
    assert (await sync_all(client, owner, team.org)).status_code == 404


async def test_sync_needs_client_update(client):
    team = await new_team(client)
    created = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    for role in ("staff", "viewer"):
        member = await team.member(role)
        assert (await sync_client(client, member, team.org, created["id"])).status_code == 403, role
        assert (await sync_all(client, member, team.org)).status_code == 403, role
    # Billing and managers may edit clients, so they may synchronise them.
    for role in ("billing", "manager"):
        member = await team.member(role)
        assert (await sync_client(client, member, team.org, created["id"])).status_code == 200, role
        assert (await sync_all(client, member, team.org)).status_code == 200, role


# --- Synchronise every linked client ----------------------------------------------------------------


async def test_sync_all_updates_the_linked_clients_that_are_not_archived(client, central_gateway):
    team = await new_team(client)
    await create_client(client, team.owner, team.org, "Local Co")
    first = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    second = (await enabled(client, team.owner, team.org, PLUMBING))["client"]
    archived = (await enabled(client, team.owner, team.org, JOINERY))["client"]
    done = await client.post(
        client_url(team, archived["id"], "/archive"), headers=team.owner.headers
    )
    assert done.status_code == 200, done.text
    central_gateway.rename(BAKERY, city="York")
    central_gateway.rename(JOINERY, city="Bradford")  # archived here: left alone
    response = await sync_all(client, team.owner, team.org)
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Clients synchronised"
    data = response.json()["data"]
    assert {
        k: data[k]
        for k in ("available", "processed", "synced", "stale", "failed", "changed", "remaining")
    } == {
        "available": True,
        "processed": 2,
        "synced": 2,
        "stale": 0,
        "failed": 0,
        "changed": 1,
        "remaining": 0,
    }
    by_id = {r["client_id"]: r for r in data["results"]}
    assert set(by_id) == {first["id"], second["id"]}
    assert by_id[first["id"]]["changes"] == {"city": ["Leeds", "York"]}
    assert by_id[second["id"]]["changes"] == {} and by_id[second["id"]]["changed"] is False
    assert (await client_row(archived["id"]))["city"] == "Halifax"
    assert len(await audit_entries(team.id, "client.central_synced")) == 1
    syncs = [r for r in await sync_logs(client, team.owner, team.org) if r["operation"] == "sync"]
    assert len(syncs) == 2


async def test_sync_all_while_central_is_down_marks_everything_stale(client, central_gateway):
    team = await new_team(client)
    await enabled(client, team.owner, team.org, BAKERY)
    await enabled(client, team.owner, team.org, PLUMBING)
    central_gateway.set_available(False)
    response = await sync_all(client, team.owner, team.org)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["available"], data["processed"], data["stale"], data["synced"]) == (False, 2, 2, 0)
    assert all(r["state"] == "stale" and r["from_cache"] for r in data["results"])
    failed = await sync_logs(client, team.owner, team.org, status="failed")
    assert len(failed) == 2
    states = await scalar(
        "SELECT count(*) FROM clients WHERE organization_id = :o AND central_sync_state = 'stale'",
        o=team.id,
    )
    assert states == 2


async def test_sync_all_is_bounded_and_takes_the_longest_unsynced_first(
    client, central_gateway, monkeypatch
):
    monkeypatch.setattr(central_sync, "MAX_SYNC_CLIENTS", 2)
    team = await new_team(client)
    ids = {}
    for central_id in (BAKERY, PLUMBING, JOINERY):
        ids[central_id] = (await enabled(client, team.owner, team.org, central_id))["client"]["id"]
    first = (await sync_all(client, team.owner, team.org)).json()["data"]
    assert (first["processed"], first["remaining"]) == (2, 1)
    assert {r["client_id"] for r in first["results"]} == {ids[BAKERY], ids[PLUMBING]}
    # The two just synchronised go to the back of the queue: the third is next.
    second = (await sync_all(client, team.owner, team.org)).json()["data"]
    assert (second["processed"], second["remaining"]) == (2, 1)
    assert ids[JOINERY] in {r["client_id"] for r in second["results"]}


async def test_sync_all_with_nothing_linked_does_nothing(client):
    team = await new_team(client)
    await create_client(client, team.owner, team.org, "Local")
    data = (await sync_all(client, team.owner, team.org)).json()["data"]
    assert data == {
        "available": True,
        "processed": 0,
        "synced": 0,
        "stale": 0,
        "failed": 0,
        "changed": 0,
        "remaining": 0,
        "results": [],
    }
