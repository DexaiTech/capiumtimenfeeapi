"""Migration 100, the MySQL baseline: one linear history, a schema equal to the models, a clean
downgrade, the permission catalogue, and the rules the database itself enforces (overlapping
periods, append-only history).

It replaces the PostgreSQL migration tests, whose migrations (``alembic/postgres_history/``)
no longer run."""

import uuid
from datetime import date

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

from app.core.permissions import CATALOGUE
from app.db.base import Base
from app.db.errors import database_error
from app.db.triggers import APPEND_ONLY, NO_OVERLAP
from tests.clients_helpers import scalar, sql
from tests.conftest import ROOT
from tests.domain_factories import practice
from tests.migration_helpers import alembic, head_revision, migrate

TABLES = "SELECT count(*) FROM information_schema.tables WHERE table_schema = DATABASE()"
TRIGGERS = (
    "SELECT trigger_name AS name FROM information_schema.triggers WHERE trigger_schema = DATABASE() "
    "ORDER BY trigger_name"
)


def expected_triggers() -> list[str]:
    names = [f"{rule.name}_{event}" for rule in NO_OVERLAP for event in ("insert", "update")]
    names += [
        f"trg_{table}_append_only_{event}"
        for table in APPEND_ONLY
        for event in ("update", "delete")
    ]
    return sorted(names)


def test_the_history_is_one_line_starting_at_the_baseline():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [head_revision()]
    revisions = list(scripts.walk_revisions())
    assert revisions[-1].revision == "100" and revisions[-1].down_revision is None
    assert all(len(r._normalized_down_revisions) <= 1 for r in revisions)


def test_the_models_match_the_migrations():
    checked = alembic("check")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "No new upgrade operations detected" in checked.stdout + checked.stderr


async def test_downgrade_removes_every_table_and_upgrade_builds_them_again():
    try:
        down = await migrate("downgrade", "base")
        assert down.returncode == 0, down.stderr
        # Only Alembic's own table is left.
        assert await scalar(TABLES) == 1
        up = await migrate("upgrade", "head")
        assert up.returncode == 0, up.stderr
    finally:
        await migrate("upgrade", "head")
    assert await scalar(TABLES) == len(Base.metadata.tables) + 1
    assert sorted(row["name"] for row in await sql(TRIGGERS)) == expected_triggers()


async def test_the_permission_catalogue_is_seeded():
    codes = {row["code"] for row in await sql("SELECT code FROM permissions")}
    assert codes == {p.code for p in CATALOGUE}


async def _refused(statement: str, **params: object) -> str:
    # IntegrityError, as on PostgreSQL (MySQL's driver says OperationalError; app/db/errors.py).
    with pytest.raises(IntegrityError) as raised:
        await sql(statement, **params)
    error = database_error(raised.value)
    assert error is not None, raised.value
    return f"{error.kind}:{error.constraint}"


async def test_the_audit_log_refuses_updates_and_deletes(client):
    _, org = await practice(client)
    assert await scalar("SELECT count(*) FROM audit_logs WHERE organization_id = :o", o=org["id"])
    assert (
        await _refused("UPDATE audit_logs SET action = 'x' WHERE organization_id = :o", o=org["id"])
        == "rejected:audit_logs is append-only"
    )
    assert (
        await _refused("DELETE FROM audit_logs WHERE organization_id = :o", o=org["id"])
        == "rejected:audit_logs is append-only"
    )


async def test_overlapping_hours_of_one_person_are_refused_by_the_database(client):
    owner, org = await practice(client)
    insert = (
        "INSERT INTO staff_hours_bases (id, organization_id, user_id, basis, standard_minutes, "
        "effective_from, effective_to) VALUES (:id, :o, :u, 'weekly', 2250, :start, :end)"
    )
    # The owner already has open-ended hours from the day they joined.
    assert (
        await scalar(
            "SELECT count(*) FROM staff_hours_bases WHERE organization_id = :o AND user_id = :u",
            o=org["id"],
            u=str(owner.id),
        )
        == 1
    )
    assert (
        await _refused(
            insert,
            id=str(uuid.uuid4()),
            o=org["id"],
            u=str(owner.id),
            start=date(2099, 1, 1),
            end=None,
        )
        == "overlap:ex_staff_hours_bases_user_period"
    )
    # Closing the open period first leaves room for the next one, which is then accepted.
    await sql(
        "UPDATE staff_hours_bases SET effective_to = :end WHERE organization_id = :o AND user_id = :u",
        end=date(2098, 12, 31),
        o=org["id"],
        u=str(owner.id),
    )
    await sql(
        insert, id=str(uuid.uuid4()), o=org["id"], u=str(owner.id), start=date(2099, 1, 1), end=None
    )
    # Moving the closed period over the new one is refused as well.
    assert (
        await _refused(
            "UPDATE staff_hours_bases SET effective_to = NULL "
            "WHERE organization_id = :o AND user_id = :u AND effective_to IS NOT NULL",
            o=org["id"],
            u=str(owner.id),
        )
        == "overlap:ex_staff_hours_bases_user_period"
    )


async def test_a_check_violation_is_an_integrity_error_naming_its_constraint(client):
    _, org = await practice(client)
    assert (
        await _refused("UPDATE organizations SET minutes_per_day = -1 WHERE id = :o", o=org["id"])
    ).startswith("check:ck_organizations_")
