"""Migration 020: period locks, the estimate sequence and the rate mode - what a downgrade takes
away and what an upgrade seeds for the practices that already exist."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models.practice import SEQUENCE_KINDS
from tests.clients_helpers import scalar, sql
from tests.domain_factories import practice
from tests.factories import API
from tests.migration_helpers import head_revision, migrate
from tests.period_lock_helpers import lock_period, org_url, today

NEW_TABLES = ("period_locks", "period_lock_events")
ESTIMATE_ROW = (
    "SELECT prefix, padding, next_number FROM document_sequences "
    "WHERE organization_id = :o AND kind = 'estimate'"
)


async def exists(table: str) -> bool:
    rows = await sql("SELECT to_regclass(:t)::text AS t", t=f"public.{table}")
    return rows[0]["t"] is not None


async def test_downgrading_removes_what_020_added_and_upgrading_seeds_every_practice(client):
    first_owner, first = await practice(client)
    _, second = await practice(client, "Second Practice")
    patched = await client.patch(
        f"{API}/organizations/{first['id']}",
        headers=first_owner.headers,
        json={"rate_mode": "grade_only"},
    )
    assert patched.status_code == 200, patched.text
    locked = await lock_period(
        client, first_owner, first, "year", today(first).year - 1, reason="Quarter end"
    )
    assert locked.status_code == 200, locked.text
    assert await scalar("SELECT count(*) FROM period_lock_events") == 1

    try:
        down = await migrate("downgrade", "019")
        assert down.returncode == 0, down.stderr
        # Everything 020 added is gone ...
        for table in NEW_TABLES:
            assert not await exists(table), table
        assert (
            await sql("SELECT 1 FROM pg_proc WHERE proname = 'reject_period_lock_event_change'")
            == []
        )
        columns = {
            row["column_name"]
            for row in await sql(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'organizations'"
            )
        }
        assert "rate_mode" not in columns
        kinds = {row["kind"] for row in await sql("SELECT kind FROM document_sequences")}
        assert kinds == set(SEQUENCE_KINDS) - {"estimate"}
        # ... the older check no longer knows the estimate kind ...
        with pytest.raises(IntegrityError, match="ck_document_sequences_kind"):
            await sql(
                "INSERT INTO document_sequences (organization_id, kind, prefix, padding, "
                "next_number) VALUES (:o, 'estimate', 'EST', 5, 1)",
                o=first["id"],
            )
        assert await sql("SELECT version_num FROM alembic_version") == [{"version_num": "019"}]
        # ... and the practices are untouched.
        assert await scalar("SELECT count(*) FROM organizations") == 2
    finally:
        up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert await sql("SELECT version_num FROM alembic_version") == [
        {"version_num": head_revision()}
    ]

    # Every practice has an empty lock, the estimate sequence and the default rate mode again.
    for org in (first, second):
        assert await sql(
            "SELECT locked_through FROM period_locks WHERE organization_id = :o", o=org["id"]
        ) == [{"locked_through": None}]
        assert await sql(ESTIMATE_ROW, o=org["id"]) == [
            {"prefix": "EST", "padding": 5, "next_number": 1}
        ]
        assert await sql("SELECT rate_mode FROM organizations WHERE id = :o", o=org["id"]) == [
            {"rate_mode": "staff_first"}
        ]
    assert await scalar("SELECT count(*) FROM period_locks") == 2
    assert await scalar("SELECT count(*) FROM period_lock_events") == 0

    # The practices can use all of it straight away.
    state = await client.get(org_url(first, "/period-locks"), headers=first_owner.headers)
    assert state.status_code == 200 and state.json()["data"]["periods"] == []
    sequences = await client.get(org_url(first, "/sequences"), headers=first_owner.headers)
    previews = {row["kind"]: row["preview"] for row in sequences.json()["data"]}
    assert previews["estimate"] == "EST00001"
    patched = await client.patch(
        f"{API}/organizations/{first['id']}",
        headers=first_owner.headers,
        json={"rate_mode": "grade_only"},
    )
    assert patched.status_code == 200 and patched.json()["data"]["rate_mode"] == "grade_only"
    assert (
        await lock_period(client, first_owner, first, "year", today(first).year - 1)
    ).status_code == 200

    # Models and migrations agree, and running the migrations again changes nothing.
    check = await migrate("check")
    assert check.returncode == 0, check.stdout + check.stderr
    assert "No new upgrade operations detected" in check.stdout + check.stderr
    again = await migrate("upgrade", "head")
    assert again.returncode == 0, again.stderr
    assert await scalar("SELECT count(*) FROM period_locks") == 2


async def test_the_head_is_the_newest_script_and_is_what_the_database_runs_at(client):
    """The suite computes the head from ``alembic/versions``: it moves as migrations are added."""
    head = head_revision()
    assert head >= "020"
    assert await sql("SELECT version_num FROM alembic_version") == [{"version_num": head}]
