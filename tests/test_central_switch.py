"""Ticket APT74: Capium Central (the central client integration) is an integration a practice
switches on in Settings, Integrations. It is off by default; while it is off every central client
route answers 409 ``INTEGRATION_DISABLED`` and nothing is synchronised. Every member of the practice
reads the switch on the practice context (``central_client_enabled``), so the UI can hide Central."""

from httpx import AsyncClient

from tests.central_helpers import (
    BAKERY,
    central_on,
    enable,
    enabled,
    link,
    new_team,
    process_changes,
    search,
    send_event,
    sync_all,
    sync_client,
    sync_logs,
    unlink,
)
from tests.clients_helpers import create_client, org_url
from tests.domain_factories import practice, staff
from tests.factories import API, Actor, audit_entries, create_org, error
from tests.time_helpers import ok_data


async def central_card(client: AsyncClient, actor: Actor, org: dict) -> dict:
    cards = ok_data(await client.get(org_url(org, "/integrations"), headers=actor.headers))
    [card] = [card for card in cards if card["key"] == "central_client"]
    return card


async def contexts(client: AsyncClient, actor: Actor, org: dict) -> list[bool]:
    """``central_client_enabled`` as each practice-context route answers it."""
    by_id = ok_data(await client.get(f"{API}/organizations/{org['id']}", headers=actor.headers))
    by_slug = ok_data(
        await client.get(f"{API}/organizations/by-slug/{org['slug']}", headers=actor.headers)
    )
    mine = ok_data(await client.get(f"{API}/users/me/organizations", headers=actor.headers))
    [listed] = [entry for entry in mine if entry["organization"]["id"] == org["id"]]
    return [
        by_id["central_client_enabled"],
        by_slug["central_client_enabled"],
        listed["central_client_enabled"],
    ]


async def test_capium_central_is_off_by_default(client):
    owner, org = await practice(client)
    card = await central_card(client, owner, org)
    assert card["enabled"] is False
    assert card["status"] == "disabled"
    assert card["always_on"] is False
    assert await contexts(client, owner, org) == [False, False, False]
    created = await client.post(
        f"{API}/organizations", headers=owner.headers, json={"name": "Second Practice"}
    )
    assert ok_data(created, 201)["central_client_enabled"] is False


async def test_switching_capium_central_on_and_off_is_saved_and_audited(client):
    owner, org = await practice(client)
    other = await create_org(client, owner, "Second Practice")
    await central_on(client, owner, org)
    assert (await central_card(client, owner, org))["enabled"] is True
    assert await contexts(client, owner, org) == [True, True, True]
    # Only that practice: the owner's other practice keeps it off.
    assert await contexts(client, owner, other) == [False, False, False]
    await central_on(client, owner, org, on=False)
    assert (await central_card(client, owner, org))["enabled"] is False
    assert await contexts(client, owner, org) == [False, False, False]
    entries = await audit_entries(org["id"], "integration.updated")
    assert [e["metadata"] for e in entries] == [
        {"integration": "central_client", "name": "Central Client", "changes": change}
        for change in ({"enabled": [False, True]}, {"enabled": [True, False]})
    ]


async def test_every_member_reads_the_switch_without_integration_read(client):
    owner, org = await practice(client)
    await central_on(client, owner, org)
    member = await staff(org)
    assert (
        await client.get(org_url(org, "/integrations"), headers=member.headers)
    ).status_code == 403
    assert await contexts(client, member, org) == [True, True, True]
    denied = await client.patch(
        org_url(org, "/integrations/central_client"),
        headers=member.headers,
        json={"enabled": False},
    )
    assert denied.status_code == 403


async def test_central_work_is_refused_while_capium_central_is_off(client):
    owner, org = await practice(client)
    local = await create_client(client, owner, org, "Local Ltd")
    refused = [
        await search(client, owner, org, q="northgate"),
        await enable(client, owner, org, BAKERY),
        await link(client, owner, org, local["id"], BAKERY),
        await unlink(client, owner, org, local["id"]),
        await sync_client(client, owner, org, local["id"]),
        await sync_all(client, owner, org),
        await send_event(client, owner, org, "archived", BAKERY),
        await process_changes(client, owner, org),
        await client.post(org_url(org, "/integrations/central_client/sync"), headers=owner.headers),
    ]
    for response in refused:
        assert response.status_code == 409, response.text
        assert error(response)["code"] == "INTEGRATION_DISABLED"
    assert await sync_logs(client, owner, org, integration="central_client") == []
    # The permission is checked first, and the mock service's own switch still answers.
    member = await staff(org)
    assert (await search(client, member, org)).status_code == 403
    mock = await client.get(
        org_url(org, "/central-clients/mock/availability"), headers=owner.headers
    )
    assert mock.status_code == 200


async def test_switching_capium_central_off_stops_changes_reaching_linked_clients(
    client, central_gateway
):
    team = await new_team(client)
    linked = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    await central_on(client, team.owner, team.org, on=False)
    central_gateway.rename(BAKERY, name="Northgate Bakehouse Ltd")
    assert (await process_changes(client, team.owner, team.org)).status_code == 409
    renamed = await send_event(client, team.owner, team.org, "renamed", BAKERY, name="Other Ltd")
    assert renamed.status_code == 409
    detail_url = org_url(team.org, f"/clients/{linked['id']}")
    detail = ok_data(await client.get(detail_url, headers=team.owner.headers))
    assert detail["name"] == linked["name"]
    # Back on, the change feed picks up where it was.
    await central_on(client, team.owner, team.org)
    assert ok_data(await process_changes(client, team.owner, team.org))["applied"] == 1
    detail = ok_data(await client.get(detail_url, headers=team.owner.headers))
    assert detail["name"] == "Northgate Bakehouse Ltd"
