"""Migration 081 (APT81, phase A): ``staff_hours_bases`` and ``overtime_rates``. Every member gets
one open-ended weekly row of their weekly capacity from their start date, else the day they
joined; going back drops both tables; the model and the migration agree."""

from datetime import date

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db import models  # noqa: F401  (registers every table on Base.metadata)
from app.db.base import Base
from tests.clients_helpers import sql
from tests.domain_factories import practice, staff
from tests.migration_helpers import head_revision, migrate

TABLES = {"staff_hours_bases", "overtime_rates"}


async def tables() -> set[str]:
    rows = await sql(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = ANY(:t)",
        t=sorted(TABLES),
    )
    return {r["table_name"] for r in rows}


async def test_upgrading_gives_every_member_their_weekly_hours_and_downgrading_drops_them(
    client,
):
    owner, org = await practice(client)
    started = await staff(org)
    joined = await staff(org)
    idle = await staff(org)
    await sql(
        "UPDATE organization_members SET start_date = '2024-04-01', weekly_capacity_minutes = 1800 "
        "WHERE user_id = :u",
        u=str(started.id),
    )
    # 23:30 UTC on 31 March is 1 April in London (summer time).
    await sql(
        "UPDATE organization_members SET start_date = NULL, "
        "joined_at = '2025-03-31 23:30:00+00' WHERE user_id = :u",
        u=str(joined.id),
    )
    await sql(
        "UPDATE organization_members SET weekly_capacity_minutes = 0 WHERE user_id = :u",
        u=str(idle.id),
    )
    try:
        down = await migrate("downgrade", "080")
        assert down.returncode == 0, down.stderr
        assert await tables() == set()
    finally:
        up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert await sql("SELECT version_num FROM alembic_version") == [
        {"version_num": head_revision()}
    ]
    assert await tables() == TABLES

    rows = await sql(
        "SELECT user_id, basis, standard_minutes, effective_from, effective_to, created_by "
        "FROM staff_hours_bases WHERE organization_id = :o",
        o=org["id"],
    )
    found = {str(r["user_id"]): r for r in rows}
    # The owner, the started and the joined member; not the one with no capacity.
    assert set(found) == {str(owner.id), str(started.id), str(joined.id)}
    assert all(
        (r["basis"], r["effective_to"], r["created_by"]) == ("weekly", None, None) for r in rows
    )
    assert (
        found[str(started.id)]["standard_minutes"],
        found[str(started.id)]["effective_from"],
    ) == (
        1800,
        date(2024, 4, 1),
    )
    assert (found[str(joined.id)]["standard_minutes"], found[str(joined.id)]["effective_from"]) == (
        2250,
        date(2025, 4, 1),
    )


def _new_objects(obj, name, type_, reflected, compare_to) -> bool:
    """Only the two tables migration 081 adds, and what is on them."""
    if type_ == "table":
        return name in TABLES
    table = getattr(obj, "table", None)
    return table is not None and table.name in TABLES


def _drift(connection) -> list:
    context = MigrationContext.configure(connection, opts={"include_object": _new_objects})
    return compare_metadata(context, Base.metadata)


async def test_the_model_and_the_migration_agree_on_the_overtime_tables(client):
    engine = create_async_engine(get_settings().direct_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            drift = await connection.run_sync(_drift)
    finally:
        await engine.dispose()
    assert drift == []
