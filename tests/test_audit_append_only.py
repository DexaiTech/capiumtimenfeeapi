"""The audit trail is append-only: a trigger refuses UPDATE and DELETE on every partition, no
endpoint and no code path changes an entry, and a practice's deletion keeps its trail."""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from tests.audit_helpers import audit_url, entry_rows, insert_entry
from tests.clients_helpers import create_client, sql
from tests.domain_factories import practice
from tests.factories import API
from tests.migration_helpers import head_revision, migrate

APP = Path(__file__).resolve().parents[1] / "app"
GUARD = "trg_audit_logs_append_only"
OLD = datetime(2001, 1, 1, 12, 0, tzinfo=UTC)  # before every monthly partition: the default one


async def partitions() -> set[str]:
    rows = await sql(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "WHERE i.inhparent = 'audit_logs'::regclass"
    )
    return {row["relname"] for row in rows}


async def guarded_tables() -> set[str]:
    rows = await sql(
        "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "WHERE t.tgname = :name AND NOT t.tgisinternal",
        name=GUARD,
    )
    return {row["relname"] for row in rows}


async def test_the_guard_is_on_the_table_and_on_every_partition():
    names = await partitions()
    assert "audit_logs_default" in names
    assert len(names) >= 5  # the default one, this month and three more
    assert await guarded_tables() == names | {"audit_logs"}


async def test_an_entry_cannot_be_updated_or_deleted(client):
    _, org = await practice(client)
    await insert_entry(org, "test.now")
    await insert_entry(org, "test.old", created_at=OLD)
    before = await entry_rows(org)
    assert [row["action"] for row in before] == ["test.old", "organization.created", "test.now"]

    for statement in (
        "UPDATE audit_logs SET action = 'edited'",
        "UPDATE audit_logs SET metadata = '{}'::jsonb WHERE action = 'test.now'",
        "UPDATE audit_logs SET actor_user_id = NULL, ip_address = NULL",
        "UPDATE audit_logs SET created_at = created_at + interval '1 day' WHERE action = 'test.old'",
        "DELETE FROM audit_logs",
        "DELETE FROM audit_logs WHERE action = 'test.old'",
        "DELETE FROM audit_logs WHERE organization_id IS NOT NULL",
    ):
        with pytest.raises(IntegrityError, match="append-only"):
            await sql(statement)
    assert await entry_rows(org) == before


async def test_a_partition_created_later_is_guarded_too(client):
    _, org = await practice(client)
    [created] = await sql("SELECT create_monthly_partition('audit_logs', DATE '2031-03-01') AS n")
    assert created["n"] == "audit_logs_2031_03"
    assert "audit_logs_2031_03" in await guarded_tables()
    await insert_entry(org, "test.future", created_at=datetime(2031, 3, 15, 9, 0, tzinfo=UTC))
    for statement in (
        "UPDATE audit_logs SET action = 'edited' WHERE action = 'test.future'",
        "DELETE FROM audit_logs WHERE action = 'test.future'",
    ):
        with pytest.raises(IntegrityError, match="append-only"):
            await sql(statement)
    assert len(await entry_rows(org, "test.future")) == 1


async def test_no_endpoint_changes_an_entry(client):
    owner, org = await practice(client)
    entry_id = await insert_entry(org, "test.entry")
    for path in ("", f"/{entry_id}", "/history", "/export"):
        for method in ("put", "patch", "delete", "post"):
            response = await client.request(
                method, audit_url(org, path), headers=owner.headers, json={}
            )
            assert response.status_code in (404, 405), (method, path)
    assert len(await entry_rows(org, "test.entry")) == 1


def test_no_code_writes_to_the_trail_except_to_add():
    patterns = (
        re.compile(r"\b(UPDATE|DELETE\s+FROM)\s+audit_logs", re.IGNORECASE),
        re.compile(r"\b(update|delete)\(\s*AuditLog\b"),
    )
    offenders = [
        path.relative_to(APP).as_posix()
        for path in sorted(APP.rglob("*.py"))
        if any(pattern.search(path.read_text(encoding="utf-8")) for pattern in patterns)
    ]
    assert offenders == []


async def test_deleting_the_practice_keeps_its_trail(client):
    owner, org = await practice(client)
    await create_client(client, owner, org, "Acme Ltd")
    response = await client.request(
        "DELETE",
        f"{API}/organizations/{org['id']}",
        headers=owner.headers,
        json={"confirm_name": org["name"]},
    )
    assert response.status_code == 200, response.text
    actions = [row["action"] for row in await entry_rows(org)]
    assert actions == ["organization.created", "client.created", "organization.deleted"]
    # Even the practice's row going away does not touch the trail: there is no cascade.
    await sql("DELETE FROM organizations WHERE id = :o", o=org["id"])
    assert [row["action"] for row in await entry_rows(org)] == actions


async def test_the_migration_reverts_and_applies_again_keeping_the_trail(client):
    _, org = await practice(client)
    await insert_entry(org, "test.kept")
    try:
        down = await migrate("downgrade", "042")
        assert down.returncode == 0, down.stderr
        assert await guarded_tables() == set()
        assert await sql("SELECT 1 FROM pg_proc WHERE proname = 'reject_audit_log_change'") == []
        indexes = await sql(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'audit_logs' "
            "AND indexname = 'ix_audit_logs_organization_id_target_created_at'"
        )
        assert indexes == []
        # Without the guard an entry can change again: the revert really removed it.
        await sql("UPDATE audit_logs SET action = 'test.edited' WHERE action = 'test.kept'")
    finally:
        up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert (await sql("SELECT version_num FROM alembic_version")) == [
        {"version_num": head_revision()}
    ]
    assert await guarded_tables() == await partitions() | {"audit_logs"}
    assert [row["action"] for row in await entry_rows(org, "test.edited")] == ["test.edited"]
    with pytest.raises(IntegrityError, match="append-only"):
        await sql("DELETE FROM audit_logs WHERE action = 'test.edited'")
    indexes = await sql(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'audit_logs' "
        "AND indexname = 'ix_audit_logs_organization_id_target_created_at'"
    )
    assert len(indexes) == 1
