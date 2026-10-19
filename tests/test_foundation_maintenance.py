"""python -m app.maintenance.reset_data"""

import os
import subprocess
import sys

from sqlalchemy import text

from app.maintenance.reset_data import KEPT_TABLES, reset_data, table_counts
from tests.conftest import ROOT
from tests.factories import add_member, create_user_with_org


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "app.maintenance.reset_data", *args],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


async def test_reset_empties_every_table_but_the_schema_and_the_catalogue(client, db):
    _, org = await create_user_with_org(client)
    await add_member(org["id"], "staff")
    permissions = await db.scalar(text("SELECT count(*) FROM permissions"))
    version = await db.scalar(text("SELECT version_num FROM alembic_version"))

    before = await table_counts(db)
    assert not KEPT_TABLES & set(before)
    assert before["organizations"] == 1 and before["vat_rates"] == 5
    deleted = await reset_data(db)
    await db.commit()
    assert deleted["users"] == 2 and deleted["organization_members"] == 2

    after = await table_counts(db)
    assert set(after.values()) == {0}
    assert await db.scalar(text("SELECT count(*) FROM permissions")) == permissions
    assert await db.scalar(text("SELECT version_num FROM alembic_version")) == version


async def _count_organizations(db) -> int:
    count = int(await db.scalar(text("SELECT count(*) FROM organizations")))
    # End the read transaction: its locks would block the command's TRUNCATE.
    await db.commit()
    return count


async def test_the_command_is_a_dry_run_without_yes(client, db):
    await create_user_with_org(client)
    dry = _run()
    assert dry.returncode == 0, dry.stderr
    assert "Dry run" in dry.stdout and "organizations: 1 rows" in dry.stdout
    assert await _count_organizations(db) == 1
    # The connection string's credentials are never printed, only host, port and database.
    assert os.environ["DB_URL"] not in dry.stdout + dry.stderr

    done = _run("--yes")
    assert done.returncode == 0, done.stderr
    assert await _count_organizations(db) == 0
    assert "Nothing to delete" in _run().stdout
