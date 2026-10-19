"""The central client service: searching it, enabling its clients, the mock's switches and the
client import."""

import asyncio

import pytest

from app.core.config import get_settings
from app.main import app
from app.services.central_clients import (
    CentralChanges,
    CentralClient,
    CentralUnavailableError,
    MockCentralClientGateway,
    get_central_gateway,
)
from tests.central_helpers import (
    ARCHIVED,
    ASHBY,
    BAKERY,
    BOOKKEEPER,
    JOINERY,
    PLUMBING,
    client_row,
    enable,
    enabled,
    new_team,
    scalar,
    search,
    sql,
    sync_logs,
)
from tests.clients_helpers import create_client, org_url
from tests.factories import audit_entries, create_user_with_org, error

# --- Search -----------------------------------------------------------------------------


async def test_search_returns_the_central_fields_live_and_fills_the_cache(client):
    team = await new_team(client)
    response = await search(client, team.owner, team.org, q="northgate")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["source"], data["stale"]) == ("live", False)
    [item] = data["items"]
    assert {k: item[k] for k in item if k not in ("updated_at", "fetched_at")} == {
        "central_id": BAKERY,
        "name": "Northgate Bakery Ltd",
        "company_number": "09231457",
        "address_line1": "12 Northgate Street",
        "address_line2": None,
        "city": "Leeds",
        "postcode": "LS1 4AB",
        "country": "GB",
        "status": "active",
        "enabled": False,
        "client_id": None,
        "client_code": None,
        "matching_client": None,
    }
    assert item["updated_at"].startswith("2025-01-06T09:00:00")
    assert item["fetched_at"] is not None
    assert (
        await scalar(
            "SELECT count(*) FROM central_client_cache WHERE organization_id = :o", o=team.id
        )
        == 1
    )
    # A blank search lists the whole catalogue (and caches it).
    everything = await search(client, team.owner, team.org, limit=50)
    assert len(everything.json()["data"]["items"]) == 25
    assert (
        await scalar(
            "SELECT count(*) FROM central_client_cache WHERE organization_id = :o", o=team.id
        )
        == 25
    )


async def test_blank_search_lists_by_name_up_to_the_limit(client):
    team = await new_team(client)
    first = await search(client, team.owner, team.org, limit=3)
    assert [i["name"] for i in first.json()["data"]["items"]] == [
        "Ashby & Partners LLP",
        "Brightwater Plumbing & Heating Ltd",
        "Calder Valley Joinery Ltd",
    ]
    default = await search(client, team.owner, team.org)
    assert len(default.json()["data"]["items"]) == 20  # the default limit


async def test_search_matches_company_numbers_ignoring_case_and_spaces(client):
    team = await new_team(client)
    for text, expected in (("09231457", BAKERY), ("oc 4123", ASHBY), ("sc654", "CC-0006")):
        found = await search(client, team.owner, team.org, q=text)
        assert [i["central_id"] for i in found.json()["data"]["items"]] == [expected], text
    # Fewer than three characters only ever match names.
    short = await search(client, team.owner, team.org, q="92")
    assert short.json()["data"]["items"] == []
    nothing = await search(client, team.owner, team.org, q="no such company")
    assert nothing.json()["data"] == {"items": [], "source": "live", "stale": False}


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": 51}, {"limit": "many"}, {"q": "x" * 201}],
)
async def test_search_validates_its_parameters(client, params):
    team = await new_team(client)
    response = await search(client, team.owner, team.org, **params)
    assert response.status_code == 422, response.text


async def test_search_shows_what_is_enabled_and_which_local_client_would_be_linked(client):
    team = await new_team(client)
    local = await create_client(
        client, team.owner, team.org, "North Gate Bakery", company_number="9231457"
    )
    found = (await search(client, team.owner, team.org, q="northgate")).json()["data"]["items"][0]
    assert found["enabled"] is False
    assert found["matching_client"] == {
        "id": local["id"],
        "code": local["code"],
        "name": "North Gate Bakery",
    }
    # Enabling adopts that client: the item now reports it as the linked one.
    adopted = await client.post(
        org_url(team.org, f"/central-clients/{BAKERY}/enable"), headers=team.owner.headers
    )
    assert adopted.status_code == 200 and adopted.json()["data"]["result"] == "linked_existing"
    found = (await search(client, team.owner, team.org, q="northgate")).json()["data"]["items"][0]
    assert (found["enabled"], found["client_id"], found["client_code"]) == (
        True,
        local["id"],
        local["code"],
    )
    assert found["matching_client"] is None


async def test_search_serves_the_cache_while_central_is_down(client, central_gateway):
    team = await new_team(client)
    live = (await search(client, team.owner, team.org, q="northgate")).json()["data"]["items"][0]
    central_gateway.set_available(False)
    response = await search(client, team.owner, team.org, q="northgate")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["source"], data["stale"]) == ("cache", True)
    [item] = data["items"]
    assert item["name"] == "Northgate Bakery Ltd" and item["company_number"] == "09231457"
    assert item["fetched_at"] == live["fetched_at"]
    # Only what was cached before: other names are not there, and nothing is invented.
    other = await search(client, team.owner, team.org, q="calder")
    assert other.status_code == 200 and other.json()["data"]["items"] == []
    assert other.json()["data"]["source"] == "cache"
    # Back up: live again.
    central_gateway.set_available(True)
    again = (await search(client, team.owner, team.org, q="calder")).json()["data"]
    assert (again["source"], len(again["items"])) == ("live", 1)


async def test_search_with_nothing_cached_while_central_is_down_is_503(client, central_gateway):
    team = await new_team(client)
    central_gateway.set_available(False)
    response = await search(client, team.owner, team.org, q="northgate")
    assert response.status_code == 503, response.text
    assert error(response)["code"] == "CENTRAL_UNAVAILABLE"
    assert response.headers["retry-after"] == "30"
    # Another practice's cache is not this one's.
    other_owner, other_org = await create_user_with_org(client, "Other Practice")
    central_gateway.set_available(True)
    await search(client, other_owner, other_org, q="northgate")
    central_gateway.set_available(False)
    assert (await search(client, team.owner, team.org)).status_code == 503


async def test_search_needs_client_create_and_a_practice_you_belong_to(client):
    team = await new_team(client)
    for role in ("staff", "viewer", "billing"):
        denied = await search(client, await team.member(role), team.org)
        assert denied.status_code == 403, role
    for role in ("manager", "partner", "admin"):
        allowed = await search(client, await team.member(role), team.org, limit=1)
        assert allowed.status_code == 200, role
    outsider, _ = await create_user_with_org(client, "Elsewhere")
    assert (await search(client, outsider, team.org)).status_code == 404
    assert (await client.get(org_url(team.org, "/central-clients"))).status_code == 401


# --- Enable -----------------------------------------------------------------------------


async def test_enable_creates_a_client_linked_to_the_central_one(client):
    team = await new_team(client)
    response = await enable(client, team.owner, team.org, BAKERY)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["message"] == "Client created from the central client service"
    data = body["data"]
    assert (data["result"], data["from_cache"]) == ("created", False)
    created = data["client"]
    assert created["code"] == "C0001"  # from the client sequence
    assert created["name"] == "Northgate Bakery Ltd"
    assert (created["status"], created["client_type"]) == ("active", "limited_company")
    assert created["company_number"] == "09231457"
    assert (created["address_line1"], created["address_line2"]) == ("12 Northgate Street", None)
    assert (created["city"], created["postcode"], created["country"]) == ("Leeds", "LS1 4AB", "GB")
    assert (created["central_client_id"], created["central_sync_state"]) == (BAKERY, "synced")
    assert created["central_synced_at"] is not None and created["merged_into_client_id"] is None

    [entry] = await audit_entries(team.id, "client.central_enabled")
    assert entry["actor_user_id"] == str(team.owner.id) and entry["target_id"] == created["id"]
    assert entry["metadata"]["central_id"] == BAKERY
    assert entry["metadata"]["result"] == "created" and entry["metadata"]["from_cache"] is False
    [log] = await sync_logs(client, team.owner, team.org, integration="central_client")
    assert (log["operation"], log["status"], log["direction"]) == ("enable", "ok", "inbound")
    assert (log["entity_type"], log["entity_id"], log["external_id"]) == (
        "client",
        created["id"],
        BAKERY,
    )
    # The next client gets the next code.
    second = await enabled(client, team.owner, team.org, PLUMBING)
    assert second["client"]["code"] == "C0002"


async def test_enable_a_sole_trader_without_a_company_number(client):
    team = await new_team(client)
    data = await enabled(client, team.owner, team.org, BOOKKEEPER)
    assert data["client"]["name"] == "Sandra Whitfield t/a Whitfield Bookkeeping"
    assert data["client"]["company_number"] is None
    assert data["client"]["client_type"] == "sole_trader"


async def test_enable_again_answers_with_the_existing_client(client):
    team = await new_team(client)
    first = await enabled(client, team.owner, team.org, BAKERY)
    again = await enable(client, team.owner, team.org, BAKERY)
    assert again.status_code == 200, again.text
    data = again.json()["data"]
    assert data["result"] == "already_enabled" and data["from_cache"] is False
    assert data["client"]["id"] == first["client"]["id"]
    assert await scalar("SELECT count(*) FROM clients WHERE organization_id = :o", o=team.id) == 1
    assert len(await audit_entries(team.id, "client.central_enabled")) == 1


async def test_enable_adopts_a_local_client_with_the_same_company_number(client):
    team = await new_team(client)
    local = await create_client(
        client,
        team.owner,
        team.org,
        "North Gate Bakery",
        company_number="9231457",  # the same number without its leading zero
        email="hello@northgate.example",
        address_line1="1 Old Road",
        city="Hull",
        postcode="HU1 1AA",
    )
    response = await enable(client, team.owner, team.org, BAKERY)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["message"] == "Existing client linked to the central client service"
    data = body["data"]
    assert data["result"] == "linked_existing"
    adopted = data["client"]
    assert adopted["id"] == local["id"] and adopted["code"] == local["code"]
    # The central values replace the name and the address; everything else stays.
    assert (adopted["name"], adopted["address_line1"], adopted["city"]) == (
        "Northgate Bakery Ltd",
        "12 Northgate Street",
        "Leeds",
    )
    assert (adopted["postcode"], adopted["email"]) == ("LS1 4AB", "hello@northgate.example")
    assert (adopted["central_client_id"], adopted["central_sync_state"]) == (BAKERY, "synced")
    assert await scalar("SELECT count(*) FROM clients WHERE organization_id = :o", o=team.id) == 1
    [entry] = await audit_entries(team.id, "client.central_enabled")
    assert entry["metadata"]["result"] == "linked_existing"
    assert entry["metadata"]["changes"] == {
        "name": ["North Gate Bakery", "Northgate Bakery Ltd"],
        "address_line1": ["1 Old Road", "12 Northgate Street"],
        "city": ["Hull", "Leeds"],
        "postcode": ["HU1 1AA", "LS1 4AB"],
    }
    # A client with another number is a different client.
    other = await enabled(client, team.owner, team.org, PLUMBING)
    assert other["result"] == "created" and other["client"]["id"] != local["id"]


async def test_enable_prefers_the_live_client_over_an_archived_one_with_the_same_number(client):
    team = await new_team(client)
    old = await create_client(
        client, team.owner, team.org, "Old Northgate", company_number="09231457"
    )
    archived = await client.patch(
        org_url(team.org, f"/clients/{old['id']}"),
        headers=team.owner.headers,
        json={"status": "archived"},
    )
    assert archived.status_code == 200, archived.text
    current = await create_client(
        client, team.owner, team.org, "Current Northgate", company_number="09231457"
    )
    response = await enable(client, team.owner, team.org, BAKERY)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["client"]["id"] == current["id"]
    assert (await client_row(old["id"]))["central_client_id"] is None


async def test_enable_unknown_archived_and_malformed_ids(client):
    team = await new_team(client)
    unknown = await enable(client, team.owner, team.org, "CC-9999")
    assert unknown.status_code == 404 and error(unknown)["code"] == "RESOURCE_NOT_FOUND"
    archived = await enable(client, team.owner, team.org, ARCHIVED)
    assert archived.status_code == 409 and error(archived)["code"] == "CENTRAL_CLIENT_ARCHIVED"
    for bad in ("CC 0001", "-x", "a" * 65):
        assert (await enable(client, team.owner, team.org, bad)).status_code == 422, bad
    assert await scalar("SELECT count(*) FROM clients WHERE organization_id = :o", o=team.id) == 0


async def test_enable_works_from_the_cache_while_central_is_down(client, central_gateway):
    team = await new_team(client)
    await search(client, team.owner, team.org, q="northgate")  # fills the cache
    central_gateway.set_available(False)
    response = await enable(client, team.owner, team.org, BAKERY)
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert (data["result"], data["from_cache"]) == ("created", True)
    assert data["client"]["name"] == "Northgate Bakery Ltd"
    assert data["client"]["central_sync_state"] == "stale"
    [entry] = await audit_entries(team.id, "client.central_enabled")
    assert entry["metadata"]["from_cache"] is True
    # A central client that was never cached cannot be enabled meanwhile.
    missing = await enable(client, team.owner, team.org, PLUMBING)
    assert missing.status_code == 503 and error(missing)["code"] == "CENTRAL_UNAVAILABLE"
    # Back up: the next synchronisation confirms the values.
    central_gateway.set_available(True)
    synced = await client.post(
        org_url(team.org, f"/clients/{data['client']['id']}/central-sync"),
        headers=team.owner.headers,
    )
    assert synced.json()["data"]["state"] == "synced"


async def test_enable_needs_client_create_and_a_practice_you_belong_to(client):
    team = await new_team(client)
    for role in ("staff", "viewer", "billing"):
        assert (await enable(client, await team.member(role), team.org, BAKERY)).status_code == 403
    assert await scalar("SELECT count(*) FROM clients WHERE organization_id = :o", o=team.id) == 0
    assert (await enable(client, await team.member("manager"), team.org, BAKERY)).status_code == 201
    outsider, _ = await create_user_with_org(client, "Elsewhere")
    assert (await enable(client, outsider, team.org, PLUMBING)).status_code == 404


async def test_the_same_central_client_can_be_enabled_by_every_practice(client):
    first = await new_team(client)
    owner, org = await create_user_with_org(client, "Second Practice")
    one = await enabled(client, first.owner, first.org, BAKERY)
    two = await enabled(client, owner, org, BAKERY)
    assert one["client"]["id"] != two["client"]["id"]
    assert one["client"]["code"] == two["client"]["code"] == "C0001"


async def test_parallel_enables_create_one_client(client):
    team = await new_team(client)
    responses = await asyncio.gather(
        *(enable(client, team.owner, team.org, BAKERY) for _ in range(6))
    )
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 200, 200, 201]
    assert {r.json()["data"]["client"]["id"] for r in responses} == {
        r.json()["data"]["client"]["id"] for r in responses
    }
    results = sorted(r.json()["data"]["result"] for r in responses)
    assert results == ["already_enabled"] * 5 + ["created"]
    assert await scalar("SELECT count(*) FROM clients WHERE organization_id = :o", o=team.id) == 1
    assert len(await audit_entries(team.id, "client.central_enabled")) == 1


async def test_parallel_enables_adopt_the_local_client_once(client):
    team = await new_team(client)
    local = await create_client(
        client, team.owner, team.org, "Local Bakery", company_number="09231457"
    )
    responses = await asyncio.gather(
        *(enable(client, team.owner, team.org, BAKERY) for _ in range(5))
    )
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert sorted(r.json()["data"]["result"] for r in responses) == [
        "already_enabled",
        "already_enabled",
        "already_enabled",
        "already_enabled",
        "linked_existing",
    ]
    assert {r.json()["data"]["client"]["id"] for r in responses} == {local["id"]}
    assert await scalar("SELECT count(*) FROM clients WHERE organization_id = :o", o=team.id) == 1


async def test_parallel_enables_of_different_clients_get_distinct_codes(client):
    team = await new_team(client)
    ids = [BAKERY, PLUMBING, JOINERY, ASHBY]
    responses = await asyncio.gather(*(enable(client, team.owner, team.org, i) for i in ids))
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    assert sorted(r.json()["data"]["client"]["code"] for r in responses) == [
        "C0001",
        "C0002",
        "C0003",
        "C0004",
    ]


# --- The mock central service ------------------------------------------------------------


async def test_the_mock_can_be_switched_off_and_on(client, central_gateway):
    team = await new_team(client)
    url = org_url(team.org, "/central-clients/mock/availability")
    on = await client.get(url, headers=team.owner.headers)
    assert on.status_code == 200 and on.json()["data"] == {"available": True, "gateway": "mock"}
    off = await client.post(url, headers=team.owner.headers, json={"available": False})
    assert off.status_code == 200, off.text
    assert off.json()["data"] == {"available": False, "gateway": "mock"}
    assert central_gateway.available is False
    assert (await client.get(url, headers=team.owner.headers)).json()["data"]["available"] is False
    assert (await search(client, team.owner, team.org)).status_code == 503
    back = await client.post(url, headers=team.owner.headers, json={"available": True})
    assert back.json()["data"]["available"] is True
    assert (await search(client, team.owner, team.org)).status_code == 200
    assert (await client.post(url, headers=team.owner.headers, json={})).status_code == 422
    assert (
        await client.post(url, headers=team.owner.headers, json={"available": "maybe"})
    ).status_code == 422


async def test_the_mock_switches_need_settings_manage_and_exist_only_for_the_mock(
    client, monkeypatch
):
    team = await new_team(client)
    url = org_url(team.org, "/central-clients/mock/availability")
    for role in ("staff", "manager", "viewer", "billing"):
        member = await team.member(role)
        assert (await client.get(url, headers=member.headers)).status_code == 403, role
        assert (
            await client.post(url, headers=member.headers, json={"available": False})
        ).status_code == 403, role
    assert (await client.get(url, headers=(await team.member("admin")).headers)).status_code == 200
    # Not available in production ...
    monkeypatch.setattr(get_settings(), "app_env", "production")
    assert (await client.get(url, headers=team.owner.headers)).status_code == 404
    pushed = await client.post(
        org_url(team.org, "/central-clients/mock/changes"),
        headers=team.owner.headers,
        json={"type": "archived", "central_id": BAKERY},
    )
    assert pushed.status_code == 404
    monkeypatch.undo()

    # ... nor for another gateway.
    class OtherGateway:
        name = "other"

        async def search(self, q: str, limit: int) -> list[CentralClient]:
            raise CentralUnavailableError("down")

        async def get(self, central_id: str) -> CentralClient | None:
            raise CentralUnavailableError("down")

        async def changes_since(self, cursor: str | None, limit: int = 100) -> CentralChanges:
            raise CentralUnavailableError("down")

    app.dependency_overrides[get_central_gateway] = OtherGateway
    try:
        assert (await client.get(url, headers=team.owner.headers)).status_code == 404
        # Any gateway that follows the interface works: this one is simply always down.
        down = await search(client, team.owner, team.org)
        assert down.status_code == 503 and error(down)["code"] == "CENTRAL_UNAVAILABLE"
    finally:
        app.dependency_overrides.pop(get_central_gateway, None)


async def test_pushing_a_change_into_the_mock(client, central_gateway):
    team = await new_team(client)
    url = org_url(team.org, "/central-clients/mock/changes")
    pushed = await client.post(
        url,
        headers=team.owner.headers,
        json={
            "type": "renamed",
            "central_id": BAKERY,
            "name": "Northgate Bakehouse Ltd",
            "city": "York",
        },
    )
    assert pushed.status_code == 201, pushed.text
    assert pushed.json()["data"] == {
        "event_id": "mock-evt-000001",
        "type": "renamed",
        "central_id": BAKERY,
    }
    changed = (await search(client, team.owner, team.org, q="bakehouse")).json()["data"]["items"]
    assert [(i["name"], i["city"]) for i in changed] == [("Northgate Bakehouse Ltd", "York")]
    archived = await client.post(
        url, headers=team.owner.headers, json={"type": "archived", "central_id": PLUMBING}
    )
    assert archived.status_code == 201
    status = (await search(client, team.owner, team.org, q="brightwater")).json()["data"]["items"]
    assert status[0]["status"] == "archived"
    merged = await client.post(
        url,
        headers=team.owner.headers,
        json={"type": "merged", "central_id": JOINERY, "into_central_id": ASHBY},
    )
    assert merged.status_code == 201 and merged.json()["data"]["event_id"] == "mock-evt-000003"


@pytest.mark.parametrize(
    "body",
    [
        {"type": "merged", "central_id": BAKERY},
        {"type": "merged", "central_id": BAKERY, "into_central_id": BAKERY},
        {"type": "renamed", "central_id": BAKERY},
        {"type": "archived", "central_id": BAKERY, "into_central_id": PLUMBING},
        {"type": "teleported", "central_id": BAKERY},
        {"type": "renamed", "central_id": "bad id", "name": "x"},
        {"type": "renamed", "central_id": BAKERY, "name": "x", "country": "Britain"},
        {"type": "renamed", "central_id": BAKERY, "name": "x", "colour": "red"},
    ],
)
async def test_a_pushed_change_is_validated(client, body):
    team = await new_team(client)
    response = await client.post(
        org_url(team.org, "/central-clients/mock/changes"), headers=team.owner.headers, json=body
    )
    assert response.status_code == 422, response.text


async def test_pushing_a_change_for_an_unknown_client_is_404(client):
    team = await new_team(client)
    url = org_url(team.org, "/central-clients/mock/changes")
    missing = await client.post(
        url, headers=team.owner.headers, json={"type": "archived", "central_id": "CC-9999"}
    )
    assert missing.status_code == 404
    bad_target = await client.post(
        url,
        headers=team.owner.headers,
        json={"type": "merged", "central_id": BAKERY, "into_central_id": "CC-9999"},
    )
    assert bad_target.status_code == 404


async def test_the_mock_catalogue_is_deterministic_and_realistic(central_gateway):
    catalogue = central_gateway.catalogue()
    assert [c.central_id for c in catalogue] == [f"CC-{n:04d}" for n in range(1, 26)]
    numbers = [c.company_number for c in catalogue if c.company_number]
    assert len(numbers) == len(set(numbers)) == 22  # unique; three have none
    assert {c.country for c in catalogue} == {"GB"}
    assert [c.central_id for c in catalogue if c.status == "archived"] == [ARCHIVED]
    assert all(c.address_line1 and c.city and c.postcode for c in catalogue)
    fresh = MockCentralClientGateway()
    assert fresh.catalogue() == catalogue


# --- The client import ----------------------------------------------------------------------


def csv_of(*lines: str) -> bytes:
    return ("\r\n".join(lines) + "\r\n").encode()


async def upload(client, actor, org, content: bytes, *, dry_run: bool):
    query = "?dry_run=true" if dry_run else ""
    return await client.post(
        org_url(org, f"/imports/clients{query}"),
        headers=actor.headers,
        files={"file": ("clients.csv", content, "text/csv")},
    )


async def test_the_import_dry_run_flags_an_enabled_central_client(client):
    team = await new_team(client)
    await enabled(client, team.owner, team.org, BAKERY)  # company number 09231457
    content = csv_of(
        "Client Name,Reg No",
        "Northgate Bakery (import),9231457",  # the same company, however it is written
        "Fresh Start Ltd,01234567",
        "No Number Ltd,",
    )
    response = await upload(client, team.owner, team.org, content, dry_run=True)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [(e["row"], e["field"], e["code"]) for e in data["errors"]] == [
        (1, "company_number", "CENTRAL_CLIENT_ALREADY_ENABLED")
    ]
    message = data["errors"][0]["message"]
    assert "Northgate Bakery Ltd (C0001)" in message and "09231457" in message
    assert (data["total_rows"], data["valid_rows"], data["created"]) == (3, 2, 0)
    # Without the offending row the file is fine.
    fine = csv_of("Client Name,Reg No", "Fresh Start Ltd,01234567", "No Number Ltd,")
    ok = await upload(client, team.owner, team.org, fine, dry_run=True)
    assert ok.status_code == 200 and ok.json()["data"]["errors"] == []


async def test_the_import_does_not_duplicate_an_enabled_central_client(client):
    team = await new_team(client)
    await enabled(client, team.owner, team.org, BAKERY)
    content = csv_of(
        "Client Name,Reg No",
        "Fresh Start Ltd,01234567",
        "Northgate Bakery (import),9231457",
    )
    refused = await upload(client, team.owner, team.org, content, dry_run=False)
    assert refused.status_code == 422, refused.text
    assert error(refused)["code"] == "IMPORT_INVALID"
    [problem] = error(refused)["details"]
    assert (problem["row"], problem["code"]) == (2, "CENTRAL_CLIENT_ALREADY_ENABLED")
    # The file is all-or-nothing: the good row was not imported either.
    assert await scalar("SELECT count(*) FROM clients WHERE organization_id = :o", o=team.id) == 1
    fine = csv_of("Client Name,Reg No", "Fresh Start Ltd,01234567", "No Number Ltd,")
    done = await upload(client, team.owner, team.org, fine, dry_run=False)
    assert done.status_code == 200, done.text
    assert done.json()["data"]["created"] == 2


async def test_the_import_also_looks_at_the_central_company_number_of_a_linked_client(client):
    team = await new_team(client)
    data = await enabled(client, team.owner, team.org, BAKERY)
    # The practice cleared the number on the client; the central one still stands.
    await sql("UPDATE clients SET company_number = NULL WHERE id = :i", i=data["client"]["id"])
    response = await upload(
        client,
        team.owner,
        team.org,
        csv_of("Client Name,Reg No", "Northgate again,09231457"),
        dry_run=True,
    )
    [problem] = response.json()["data"]["errors"]
    assert problem["code"] == "CENTRAL_CLIENT_ALREADY_ENABLED"
    # A client that is only local keeps being importable twice (the number is not unique).
    local = await create_client(
        client, team.owner, team.org, "Local Ltd", company_number="11111111"
    )
    again = await upload(
        client,
        team.owner,
        team.org,
        csv_of("Client Name,Reg No", "Local twin,11111111"),
        dry_run=False,
    )
    assert again.json()["data"]["errors"] == [] and again.json()["data"]["created"] == 1
    assert local["company_number"] == "11111111"
