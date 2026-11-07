"""python -m app.maintenance.seed_leads: sample leads for local and staging databases."""

import argparse

import pytest

from app.core.config import get_settings
from app.maintenance.seed_leads import MARKER, SeedError, _main, seed_leads
from tests.clients_helpers import org_url, scalar, sql
from tests.domain_factories import practice
from tests.factories import API, audit_entries
from tests.time_helpers import ok_data


async def _summary(client, owner, org: dict) -> dict[str, int]:
    data = ok_data(await client.get(org_url(org, "/leads/summary"), headers=owner.headers))
    return {status: data[status]["count"] for status in ("open", "converted", "archived")}


async def _lead_count(org: dict) -> int:
    return int(await scalar("SELECT count(*) FROM leads WHERE organization_id = :o", o=org["id"]))


async def _use_branches(client, owner, org: dict) -> dict:
    created = await client.post(
        org_url(org, "/branches"), headers=owner.headers, json={"name": "Leeds"}
    )
    assert created.status_code == 201, created.text
    switched = await client.patch(
        f"{API}/organizations/{org['id']}", headers=owner.headers, json={"branches_enabled": True}
    )
    assert switched.status_code == 200, switched.text
    return created.json()["data"]


async def test_a_dry_run_writes_nothing(client, db):
    owner, org = await practice(client)
    result = await seed_leads(db, practice_slug=org["slug"])
    assert not result.applied and len(result.added) == 12 and not result.skipped
    assert result.owner_email == owner.email
    assert await _lead_count(org) == 0
    assert not [e for e in await audit_entries(org["id"]) if e["action"].startswith("lead.")]


async def test_yes_adds_open_converted_and_archived_leads_as_the_owner(client, db):
    owner, org = await practice(client)
    result = await seed_leads(db, practice_slug=org["slug"], apply=True)
    assert result.applied and len(result.added) == 12
    assert await _summary(client, owner, org) == {"open": 8, "converted": 2, "archived": 2}
    notes = await sql("SELECT notes FROM leads WHERE organization_id = :o", o=org["id"])
    assert all(row["notes"].startswith(MARKER) for row in notes)
    for action, count in (
        ("lead.created", 12),
        ("lead.converted", 2),
        ("lead.archived", 2),
        ("client.created", 2),
    ):
        entries = await audit_entries(org["id"], action)
        assert len(entries) == count, action
        assert {e["actor_user_id"] for e in entries} == {str(owner.id)}


async def test_a_second_run_adds_nothing(client, db):
    _, org = await practice(client)
    await seed_leads(db, practice_slug=org["slug"], apply=True)
    again = await seed_leads(db, practice_slug=org["slug"], count=14, apply=True)
    # Only the two samples beyond the default twelve are new.
    assert [s.name for s in again.added] == ["Willow & Thyme Florists", "Ashworth Haulage Ltd"]
    third = await seed_leads(db, practice_slug=org["slug"], count=14, apply=True)
    assert not third.added and len(third.skipped) == 14
    assert await _lead_count(org) == 14
    assert len(await audit_entries(org["id"], "lead.created")) == 14


async def test_it_refuses_in_production(client, db, monkeypatch, capsys):
    _, org = await practice(client)
    monkeypatch.setattr(get_settings(), "app_env", "production")
    with pytest.raises(SeedError, match="production"):
        await seed_leads(db, practice_slug=org["slug"], apply=True)
    args = argparse.Namespace(practice_slug=org["slug"], count=12, yes=True)
    assert await _main(args) == 1
    assert "production" in capsys.readouterr().err
    assert await _lead_count(org) == 0


async def test_with_branches_the_conversions_take_the_first_active_branch(client, db):
    owner, org = await practice(client)
    branch = await _use_branches(client, owner, org)
    result = await seed_leads(db, practice_slug=org["slug"], apply=True)
    assert result.conversion_skipped is None
    assert await _summary(client, owner, org) == {"open": 8, "converted": 2, "archived": 2}
    rows = await sql(
        "SELECT c.branch_id FROM leads l JOIN clients c ON c.id = l.converted_client_id "
        "WHERE l.organization_id = :o",
        o=org["id"],
    )
    assert [str(r["branch_id"]) for r in rows] == [branch["id"]] * 2


async def test_with_branches_but_none_active_the_leads_stay_open(client, db):
    owner, org = await practice(client)
    branch = await _use_branches(client, owner, org)
    # The API keeps one branch active while branches are on; the database can still lose it.
    await sql("UPDATE branches SET is_active = false WHERE id = :b", b=branch["id"])
    result = await seed_leads(db, practice_slug=org["slug"], apply=True)
    assert result.conversion_skipped and "no active branch" in result.conversion_skipped
    assert await _summary(client, owner, org) == {"open": 10, "converted": 0, "archived": 2}
    assert not await audit_entries(org["id"], "lead.converted")
