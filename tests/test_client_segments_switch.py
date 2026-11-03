"""Ticket APT70: a practice can switch client segments off. The setting is on by default, changed
like every practice setting (``organization.update``, audited), and off it leaves the segment
column out of the client export while every segment value is kept."""

import csv
import io

from tests.clients_helpers import create_client, org_url
from tests.domain_factories import practice, staff
from tests.factories import API, audit_entries


async def patch_org(client, actor, org: dict, **body):
    return await client.patch(f"{API}/organizations/{org['id']}", headers=actor.headers, json=body)


async def export_rows(client, actor, org: dict) -> list[dict[str, str]]:
    response = await client.get(org_url(org, "/clients/export"), headers=actor.headers)
    assert response.status_code == 200, response.text
    return list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))


async def test_client_segments_are_on_by_default(client):
    owner, org = await practice(client)
    assert org["client_segments_enabled"] is True
    fresh = await client.get(f"{API}/organizations/{org['id']}", headers=owner.headers)
    assert fresh.json()["data"]["organization"]["client_segments_enabled"] is True


async def test_switching_segments_off_is_saved_and_audited(client):
    owner, org = await practice(client)
    response = await patch_org(client, owner, org, client_segments_enabled=False)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["client_segments_enabled"] is False
    [entry] = await audit_entries(org["id"], "organization.updated")
    assert entry["metadata"]["changes"] == {"client_segments_enabled": [True, False]}

    back = await patch_org(client, owner, org, client_segments_enabled=True)
    assert back.json()["data"]["client_segments_enabled"] is True


async def test_the_switch_needs_organization_update_and_a_boolean(client):
    owner, org = await practice(client)
    member = await staff(org)
    refused = await patch_org(client, member, org, client_segments_enabled=False)
    assert refused.status_code == 403, refused.text
    assert (await patch_org(client, owner, org, client_segments_enabled=None)).status_code == 422


async def test_switched_off_the_export_has_no_segment_column_and_the_values_are_kept(client):
    owner, org = await practice(client)
    acme = await create_client(client, owner, org, "Acme Widgets Ltd", segment="Gold")
    assert (await export_rows(client, owner, org))[0]["segment"] == "Gold"

    await patch_org(client, owner, org, client_segments_enabled=False)
    [row] = await export_rows(client, owner, org)
    assert "segment" not in row and row["name"] == "Acme Widgets Ltd"
    detail = await client.get(org_url(org, f"/clients/{acme['id']}"), headers=owner.headers)
    assert detail.json()["data"]["segment"] == "Gold"

    await patch_org(client, owner, org, client_segments_enabled=True)
    assert (await export_rows(client, owner, org))[0]["segment"] == "Gold"
