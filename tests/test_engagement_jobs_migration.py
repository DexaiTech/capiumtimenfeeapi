"""Migration 051 on a practice that already has engagements: the way down and up again."""

from datetime import date, timedelta

from tests.clients_helpers import sql
from tests.domain_factories import make_client, practice
from tests.migration_helpers import head_revision
from tests.test_engagement_jobs import engage, make_service, run_worker
from tests.test_ticket_migration import migrate


async def test_the_columns_and_table_go_with_the_way_down_and_come_back(client) -> None:
    owner, org = await practice(client)
    customer = await make_client(org)
    service = await make_service(client, owner, org)
    start = (date.today() - timedelta(days=2)).isoformat()
    engagement = await engage(
        client, owner, org, customer, service, frequency="monthly", start_date=start,
        auto_create_jobs=True, job_lead_days=3,
    )  # fmt: skip
    await run_worker()
    assert len(await sql("SELECT 1 FROM engagement_job_runs")) == 1
    try:
        down = await migrate("downgrade", "050")
        assert down.returncode == 0, down.stderr
        assert (await sql("SELECT to_regclass('public.engagement_job_runs')::text AS t"))[0][
            "t"
        ] is None
        columns = {
            r["column_name"]
            for r in await sql(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'client_services'"
            )
        }
        assert not {"auto_create_jobs", "job_lead_days"} & columns
        # The engagement and its job are untouched.
        assert (
            len(await sql("SELECT 1 FROM client_services WHERE id = :e", e=engagement["id"])) == 1
        )
        assert len(await sql("SELECT 1 FROM jobs")) == 1
    finally:
        up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert (await sql("SELECT version_num FROM alembic_version"))[0][
        "version_num"
    ] == head_revision()
    [row] = await sql(
        "SELECT auto_create_jobs, job_lead_days FROM client_services WHERE id = :e",
        e=engagement["id"],
    )
    assert (row["auto_create_jobs"], row["job_lead_days"]) == (False, 0)
    assert await sql("SELECT 1 FROM engagement_job_runs") == []
