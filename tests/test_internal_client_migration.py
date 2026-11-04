"""Migration 078: every practice gets one built-in Internal client with a non-billable job per
active non-chargeable activity code, and going back removes it unless time is on it."""

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
from tests.time_helpers import log_time


async def internal_clients(org: dict) -> list[dict]:
    return await sql(
        "SELECT id, code, name, status FROM clients WHERE organization_id = :o AND is_internal",
        o=org["id"],
    )


async def internal_jobs(client_id) -> dict[str, tuple[str, str, str]]:
    rows = await sql(
        "SELECT number, name, billing_basis, status FROM jobs WHERE client_id = :c", c=client_id
    )
    return {r["number"]: (r["name"], r["billing_basis"], r["status"]) for r in rows}


async def test_the_migration_gives_every_practice_one_internal_client_and_its_jobs(client):
    _, plain = await practice(client)
    _, busy = await practice(client, "Busy Practice")
    # Plain: a code of its own that is not chargeable, and holiday switched off.
    await sql(
        "INSERT INTO activity_codes (id, organization_id, code, name, is_chargeable) "
        "VALUES (gen_random_uuid(), :o, 'SICK', 'Sickness', false)",
        o=plain["id"],
    )
    await sql(
        "UPDATE activity_codes SET is_active = false WHERE organization_id = :o AND code = 'HOL'",
        o=plain["id"],
    )
    # Busy: time on its Internal client.
    person = await staff(busy)
    entry = await log_time(client, person, busy)
    [kept] = await internal_clients(busy)

    try:
        down = await migrate("downgrade", "077")
        assert down.returncode == 0, down.stderr
        columns = {
            r["column_name"]
            for r in await sql(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'clients'"
            )
        }
        assert "is_internal" not in columns
        # Plain's Internal client and jobs are gone; Busy's stays, with its time.
        assert await sql("SELECT 1 FROM clients WHERE organization_id = :o", o=plain["id"]) == []
        assert await sql("SELECT 1 FROM jobs WHERE organization_id = :o", o=plain["id"]) == []
        assert await sql("SELECT id FROM clients WHERE organization_id = :o", o=busy["id"]) == [
            {"id": kept["id"]}
        ]
        assert len(await sql("SELECT 1 FROM jobs WHERE client_id = :c", c=kept["id"])) == 4
        assert await sql("SELECT client_id FROM time_entries WHERE id = :i", i=entry["id"]) == [
            {"client_id": kept["id"]}
        ]
    finally:
        up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert (await sql("SELECT version_num FROM alembic_version")) == [
        {"version_num": head_revision()}
    ]

    [internal] = await internal_clients(plain)
    assert (internal["code"], internal["name"], internal["status"]) == (
        "INTERNAL",
        "Internal",
        "active",
    )
    assert await internal_jobs(internal["id"]) == {
        "INT-ADM": ("Practice administration", "non_billable", "in_progress"),
        "INT-TRN": ("Training and CPD", "non_billable", "in_progress"),
        "INT-BDV": ("Business development", "non_billable", "in_progress"),
        "INT-SICK": ("Sickness", "non_billable", "in_progress"),
    }

    # Busy's old one is an ordinary client now, keeping its code and job numbers; the new one
    # takes other ones.
    [fresh] = await internal_clients(busy)
    assert fresh["id"] != kept["id"]
    assert fresh["code"].startswith("INTERNAL-") and len(fresh["code"]) == len("INTERNAL-") + 8
    assert await sql("SELECT code, is_internal FROM clients WHERE id = :i", i=kept["id"]) == [
        {"code": "INTERNAL", "is_internal": False}
    ]
    fresh_jobs = await internal_jobs(fresh["id"])
    assert sorted(name for name, *_ in fresh_jobs.values()) == [
        "Business development",
        "Holiday and absence",
        "Practice administration",
        "Training and CPD",
    ]
    assert all(number.startswith("INT-") and "-" in number[4:] for number in fresh_jobs)
    assert set(await internal_jobs(kept["id"])) == {"INT-ADM", "INT-TRN", "INT-BDV", "INT-HOL"}


def _new_objects(obj, name, type_, reflected, compare_to) -> bool:
    """The clients table, and of it only what migration 078 adds."""
    if type_ == "table":
        return name == "clients"
    return name in {"is_internal", "uq_clients_organization_id_internal"}


def _drift(connection) -> list:
    context = MigrationContext.configure(connection, opts={"include_object": _new_objects})
    return compare_metadata(context, Base.metadata)


async def test_the_model_and_the_migration_agree_on_the_internal_flag(client):
    engine = create_async_engine(get_settings().direct_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            drift = await connection.run_sync(_drift)
    finally:
        await engine.dispose()
    assert drift == []
