"""Migration 015 on a practice that already exists: the way down and the way up again."""

import asyncio
import os
import subprocess
import sys

from app.core.permissions import ALL_CODES, SYSTEM_ROLES
from app.db.session import dispose_engine
from tests.clients_helpers import sql
from tests.conftest import ROOT
from tests.migration_helpers import head_revision
from tests.ticket_helpers import make_ticket, new_team, ticket_url

TICKET_TABLES = (
    "tickets",
    "ticket_tags",
    "ticket_tag_links",
    "ticket_comments",
    "ticket_attachments",
    "ticket_status_history",
)
DEFAULT_TAGS = {
    "Bug": "red",
    "Enhancement": "violet",
    "UI": "blue",
    "Reporting": "green",
    "Accounts": "blue",
    "Validation": "red",
    "Trial Balance": "violet",
}


def alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


async def migrate(*args: str) -> subprocess.CompletedProcess[str]:
    """Run alembic off the event loop, then drop pooled connections: they hold prepared
    statements of the schema as it was."""
    try:
        return await asyncio.to_thread(alembic, *args)
    finally:
        await dispose_engine()


async def role_grants(org_id: str) -> dict[str, set[str]]:
    """The permission codes of each role of the practice, by system key (or name)."""
    rows = await sql(
        "SELECT coalesce(r.system_key, r.name) AS role, p.code FROM roles r "
        "JOIN role_permissions rp ON rp.role_id = r.id "
        "JOIN permissions p ON p.id = rp.permission_id "
        "WHERE r.organization_id = :o",
        o=org_id,
    )
    grants: dict[str, set[str]] = {}
    for row in rows:
        grants.setdefault(row["role"], set()).add(row["code"])
    return grants


async def test_existing_practices_get_the_sequence_the_tags_and_the_permissions(client):
    first = await new_team(client)
    second = await new_team(client, "Second Practice")
    created = await client.post(
        first.url("/roles"),
        headers=first.owner.headers,
        json={"name": "Reception", "permissions": ["organization.read", "client.read"]},
    )
    assert created.status_code == 201, created.text
    ticket = await make_ticket(client, first.owner, first.org, "Gone with the downgrade")
    assert ticket["code"] == "APT1"

    try:
        down = await migrate("downgrade", "014")
        assert down.returncode == 0, down.stderr
        # Everything of 015 is gone: the tables, the sequence and the four permissions.
        for table in TICKET_TABLES:
            assert (await sql(f"SELECT to_regclass('public.{table}')::text AS t"))[0]["t"] is None
        assert await sql("SELECT 1 FROM document_sequences WHERE kind = 'ticket'") == []
        assert await sql("SELECT 1 FROM permissions WHERE code LIKE 'ticket.%'") == []
        revision = await sql("SELECT version_num FROM alembic_version")
        assert revision == [{"version_num": "014"}]
        # The practices, their people and their other roles are untouched.
        assert len(await sql("SELECT 1 FROM organizations")) == 2
        # The Owner still holds every permission that exists at revision 014 (later migrations
        # add their own codes, and their downgrades take them away again).
        existing = {row["code"] for row in await sql("SELECT code FROM permissions")}
        assert (await role_grants(first.id))["owner"] == existing
        assert {"client.read", "invoice.issue", "role.manage"} <= existing
    finally:
        up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr

    for team in (first, second):
        sequence = await sql(
            "SELECT prefix, padding, next_number FROM document_sequences "
            "WHERE organization_id = :o AND kind = 'ticket'",
            o=team.id,
        )
        assert sequence == [{"prefix": "APT", "padding": 1, "next_number": 1}]

        tags = await sql(
            "SELECT name, color FROM ticket_tags WHERE organization_id = :o", o=team.id
        )
        assert {row["name"]: row["color"] for row in tags} == DEFAULT_TAGS

        # Every system role holds exactly what the catalogue says.
        grants = await role_grants(team.id)
        for role in SYSTEM_ROLES:
            assert grants[role.key] == set(role.permissions), role.key
    # A custom role got nothing it was not given.
    assert (await role_grants(first.id))["Reception"] == {"organization.read", "client.read"}

    permissions = await sql("SELECT code FROM permissions")
    assert {row["code"] for row in permissions} == set(ALL_CODES)
    assert (await sql("SELECT version_num FROM alembic_version"))[0][
        "version_num"
    ] == head_revision()

    # The practices can use the tracker straight away, each person as their role allows.
    staff = await first.member("staff")
    viewer = await first.member("viewer")
    created = await client.post(
        ticket_url(first.org), headers=staff.headers, json={"subject": "First after the migration"}
    )
    assert created.status_code == 201 and created.json()["data"]["code"] == "APT1"
    assert (
        await client.post(ticket_url(first.org), headers=viewer.headers, json={"subject": "x"})
    ).status_code == 403
    listed = await client.get(ticket_url(first.org), headers=viewer.headers)
    assert listed.status_code == 200 and len(listed.json()["data"]) == 1
    tags = await client.get(first.url("/ticket-tags"), headers=viewer.headers)
    assert len(tags.json()["data"]) == 7

    # Models and migrations agree, and running the migration again changes nothing.
    check = await migrate("check")
    assert check.returncode == 0, check.stdout + check.stderr
    assert "No new upgrade operations detected" in check.stdout + check.stderr
    again = await migrate("upgrade", "head")
    assert (
        again.returncode == 0 and (await sql("SELECT count(*) AS n FROM ticket_tags"))[0]["n"] == 14
    )
