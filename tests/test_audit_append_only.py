"""The audit trail is append-only: triggers refuse UPDATE and DELETE (MySQL: the table is no longer
partitioned), no endpoint and no code path changes an entry, and a practice's deletion keeps its
trail."""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

from app.db.errors import database_error
from tests.audit_helpers import audit_url, entry_rows, insert_entry
from tests.clients_helpers import create_client, sql
from tests.domain_factories import practice
from tests.factories import API
from tests.migration_helpers import head_revision, migrate

APP = Path(__file__).resolve().parents[1] / "app"
GUARD = "trg_audit_logs_append_only"
OLD = datetime(2001, 1, 1, 12, 0, tzinfo=UTC)  # long before the trail began


async def guards() -> set[tuple[str, str]]:
    """(table, event) of every append-only guard trigger of the audit log."""
    rows = await sql(
        "SELECT event_object_table AS t, event_manipulation AS e FROM information_schema.triggers "
        "WHERE trigger_schema = DATABASE() AND trigger_name LIKE :name",
        name=f"{GUARD}%",
    )
    return {(row["t"], row["e"]) for row in rows}


async def refused(statement: str) -> None:
    """The statement is refused by the append-only guard."""
    with pytest.raises(DBAPIError, match="append-only") as raised:
        await sql(statement)
    error = database_error(raised.value)
    assert error is not None and error.kind == "rejected", raised.value


async def test_the_guard_is_on_the_table_which_is_not_partitioned():
    assert await guards() == {("audit_logs", "UPDATE"), ("audit_logs", "DELETE")}
    # No partitions any more: one table holds every entry, so one pair of triggers guards it all.
    partitioned = await sql(
        "SELECT partition_name AS p FROM information_schema.partitions "
        "WHERE table_schema = DATABASE() AND table_name = 'audit_logs' "
        "AND partition_name IS NOT NULL"
    )
    assert partitioned == []


async def test_an_entry_cannot_be_updated_or_deleted(client):
    _, org = await practice(client)
    await insert_entry(org, "test.now")
    await insert_entry(org, "test.old", created_at=OLD)
    before = await entry_rows(org)
    assert [row["action"] for row in before] == ["test.old", "organization.created", "test.now"]

    for statement in (
        "UPDATE audit_logs SET action = 'edited'",
        "UPDATE audit_logs SET metadata = JSON_OBJECT() WHERE action = 'test.now'",
        "UPDATE audit_logs SET actor_user_id = NULL, ip_address = NULL",
        "UPDATE audit_logs SET created_at = created_at + INTERVAL 1 DAY WHERE action = 'test.old'",
        "DELETE FROM audit_logs",
        "DELETE FROM audit_logs WHERE action = 'test.old'",
        "DELETE FROM audit_logs WHERE organization_id IS NOT NULL",
    ):
        await refused(statement)
    assert await entry_rows(org) == before


async def test_an_entry_dated_years_ahead_is_guarded_too(client):
    # PostgreSQL kept a partition per month; whatever the date, the one table's guard applies.
    _, org = await practice(client)
    await insert_entry(org, "test.future", created_at=datetime(2031, 3, 15, 9, 0, tzinfo=UTC))
    for statement in (
        "UPDATE audit_logs SET action = 'edited' WHERE action = 'test.future'",
        "DELETE FROM audit_logs WHERE action = 'test.future'",
    ):
        await refused(statement)
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
    # Even the practice's row going away could not touch the trail: it has no foreign key, so
    # nothing cascades to it. (MySQL refuses a plain DELETE of a practice that has clients and
    # jobs, because it checks the composite keys row by row, so this is read from the schema.)
    keys = await sql(
        "SELECT constraint_name AS c FROM information_schema.referential_constraints "
        "WHERE constraint_schema = DATABASE() AND table_name = 'audit_logs'"
    )
    assert keys == []
    assert [row["action"] for row in await entry_rows(org)] == actions


async def test_the_migration_reverts_and_applies_again_with_the_guard(client):
    # The MySQL schema is one baseline migration, so reverting it removes the trail with every
    # other table; what still holds is that the guard goes with it and comes back with it.
    _, org = await practice(client)
    await insert_entry(org, "test.kept")
    try:
        down = await migrate("downgrade", "base")
        assert down.returncode == 0, down.stderr
        assert await guards() == set()
        tables = await sql(
            "SELECT table_name AS t FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'audit_logs'"
        )
        assert tables == []
    finally:
        up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert (await sql("SELECT version_num FROM alembic_version")) == [
        {"version_num": head_revision()}
    ]
    assert await guards() == {("audit_logs", "UPDATE"), ("audit_logs", "DELETE")}
    _, org = await practice(client)
    await insert_entry(org, "test.again")
    await refused("DELETE FROM audit_logs WHERE action = 'test.again'")
    indexes = await sql(
        "SELECT DISTINCT index_name AS i FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = 'audit_logs' "
        "AND index_name = 'ix_audit_logs_organization_id_target_created_at'"
    )
    assert len(indexes) == 1
