"""Migration 082 (APT81, phases B-D): ``time_entry_overtime``, the permission ``report.staff_cost``
(Owner, Admin and Partner) and working hours for members who had none; going back drops the
table and the permission; the model and the migration agree."""

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

TABLE = "time_entry_overtime"


async def holders(org_id: str) -> set[str]:
    rows = await sql(
        "SELECT r.system_key FROM roles r JOIN role_permissions rp ON rp.role_id = r.id "
        "JOIN permissions p ON p.id = rp.permission_id AND p.code = 'report.staff_cost' "
        "WHERE r.organization_id = :o",
        o=org_id,
    )
    return {r["system_key"] for r in rows}


async def table_exists() -> bool:
    rows = await sql(
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = :t",
        t=TABLE,
    )
    return bool(rows)


async def test_upgrading_adds_the_table_the_permission_and_missing_hours(client):
    _, org = await practice(client)
    newcomer = await staff(org)  # added straight into the database: no working hours
    assert await holders(org["id"]) == {"owner", "admin", "partner"}
    try:
        down = await migrate("downgrade", "081")
        assert down.returncode == 0, down.stderr
        assert not await table_exists()
        assert await sql("SELECT 1 FROM permissions WHERE code = 'report.staff_cost'") == []
    finally:
        up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert await sql("SELECT version_num FROM alembic_version") == [
        {"version_num": head_revision()}
    ]
    assert await table_exists()
    assert await holders(org["id"]) == {"owner", "admin", "partner"}
    rows = await sql(
        "SELECT basis, standard_minutes, effective_to FROM staff_hours_bases WHERE user_id = :u",
        u=str(newcomer.id),
    )
    assert rows == [{"basis": "weekly", "standard_minutes": 2250, "effective_to": None}]


def _ours(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name == TABLE
    table = getattr(obj, "table", None)
    return table is not None and table.name == TABLE


def _drift(connection) -> list:
    context = MigrationContext.configure(connection, opts={"include_object": _ours})
    return compare_metadata(context, Base.metadata)


async def test_the_model_and_the_migration_agree(client):
    engine = create_async_engine(get_settings().direct_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            drift = await connection.run_sync(_drift)
    finally:
        await engine.dispose()
    assert drift == []
