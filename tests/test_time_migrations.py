"""Migrations 022 and 023: the internal note, the correction history and the reminders."""

import pytest
from sqlalchemy.exc import IntegrityError

from tests.clients_helpers import sql
from tests.domain_factories import make_client, practice, staff
from tests.migration_helpers import head_revision, migrate
from tests.time_gap_helpers import approved_entry, correct, history_rows
from tests.time_helpers import log_time

NEW_TABLES = ("time_entry_history", "timesheet_reminders")


async def exists(table: str) -> bool:
    rows = await sql("SELECT to_regclass(:t)::text AS t", t=f"public.{table}")
    return rows[0]["t"] is not None


async def columns(table: str) -> set[str]:
    rows = await sql(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t", t=table
    )
    return {row["column_name"] for row in rows}


async def test_the_migrations_revert_and_apply_again_keeping_the_time(client):
    owner, org = await practice(client)
    person = await staff(org)
    approver = await staff(org, "manager")
    acme = await make_client(org)
    entry = await approved_entry(client, org, person, approver, client_id=acme.id)
    await correct(client, approver, org, entry["id"], reason="Fix", minutes=90)
    await log_time(client, owner, org, internal_note="A note", entry_date="2026-09-08")
    assert len(await history_rows(org, entry["id"])) == 1
    try:
        down = await migrate("downgrade", "021")
        assert down.returncode == 0, down.stderr
        for table in NEW_TABLES:
            assert not await exists(table), table
        assert "internal_note" not in await columns("time_entries")
        left = await sql(
            "SELECT 1 FROM pg_proc WHERE proname = 'prevent_time_entry_history_change'"
        )
        assert left == []
        # The time itself is untouched.
        assert len(await sql("SELECT id FROM time_entries")) == 2
        assert (await sql("SELECT version_num FROM alembic_version")) == [{"version_num": "021"}]
    finally:
        up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert (await sql("SELECT version_num FROM alembic_version")) == [
        {"version_num": head_revision()}
    ]
    for table in NEW_TABLES:
        assert await exists(table), table
    assert "internal_note" in await columns("time_entries")
    # Time that was there before has no note; the new tables start empty.
    assert await sql("SELECT 1 FROM time_entries WHERE internal_note IS NOT NULL") == []
    assert await sql("SELECT 1 FROM time_entry_history") == []
    triggers = await sql(
        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'time_entry_history'::regclass "
        "AND NOT tgisinternal"
    )
    assert triggers == [{"tgname": "trg_time_entry_history_append_only"}]


async def test_the_note_and_reminder_constraints(client):
    _, org = await practice(client)
    person = await staff(org)
    entry = await log_time(client, person, org)
    with pytest.raises(IntegrityError, match="ck_time_entries_internal_note_length"):
        await sql(
            "UPDATE time_entries SET internal_note = repeat('n', 2001) WHERE id = :i", i=entry["id"]
        )
    await sql(
        "UPDATE time_entries SET internal_note = repeat('n', 2000) WHERE id = :i", i=entry["id"]
    )

    insert = (
        "INSERT INTO timesheet_reminders (id, organization_id, user_id, week_start, kind) "
        "VALUES (gen_random_uuid(), :o, :u, '2026-09-07', :k)"
    )
    params = {"o": org["id"], "u": str(person.id)}
    with pytest.raises(IntegrityError, match="ck_timesheet_reminders_kind"):
        await sql(insert, k="nagging", **params)
    await sql(insert, k="timesheet", **params)
    await sql(insert, k="approval", **params)  # another kind of the same week is its own reminder
    with pytest.raises(IntegrityError, match="uq_timesheet_reminders"):
        await sql(insert, k="timesheet", **params)
    assert len(await sql("SELECT id FROM timesheet_reminders")) == 2
