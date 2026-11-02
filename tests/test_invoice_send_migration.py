"""Migration 068: ``invoice.send`` goes wherever ``invoice.issue`` was, so nothing changes."""

from app.core.permissions import SYSTEM_ROLES
from tests.clients_helpers import sql
from tests.domain_factories import practice, staff
from tests.foundation_helpers import add_custom_role
from tests.migration_helpers import head_revision, migrate
from tests.test_member_permissions import put
from tests.test_ticket_migration import role_grants


async def overrides(org: dict) -> set[tuple]:
    rows = await sql(
        "SELECT user_id, permission_code, effect FROM member_permission_overrides "
        "WHERE organization_id = :o AND permission_code LIKE 'invoice.%'",
        o=org["id"],
    )
    return {(row["user_id"], row["permission_code"], row["effect"]) for row in rows}


async def test_whoever_could_issue_can_send_after_the_migration(client):
    owner, org = await practice(client)
    await add_custom_role(org["id"], "Issuers", ["invoice.read", "invoice.issue"])
    await add_custom_role(org["id"], "Readers", ["invoice.read"])
    granted = await staff(org, "manager")
    denied = await staff(org, "billing")
    await put(client, owner, org, granted, grants=["invoice.issue"])
    await put(client, owner, org, denied, denies=["invoice.issue"])

    try:
        down = await migrate("downgrade", "067")
        assert down.returncode == 0, down.stderr
        assert await sql("SELECT 1 FROM permissions WHERE code = 'invoice.send'") == []
        described = await sql("SELECT description FROM permissions WHERE code = 'invoice.issue'")
        assert described == [{"description": "Issue, send and void invoices"}]
    finally:
        up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert (await sql("SELECT version_num FROM alembic_version")) == [
        {"version_num": head_revision()}
    ]

    grants = await role_grants(org["id"])
    for role in SYSTEM_ROLES:
        assert grants[role.key] == set(role.permissions), role.key
    assert grants["Issuers"] == {"invoice.read", "invoice.issue", "invoice.send"}
    assert grants["Readers"] == {"invoice.read"}
    assert await overrides(org) == {
        (granted.id, "invoice.issue", "grant"),
        (granted.id, "invoice.send", "grant"),
        (denied.id, "invoice.issue", "deny"),
        (denied.id, "invoice.send", "deny"),
    }
