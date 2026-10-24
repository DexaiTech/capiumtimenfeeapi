"""Migration 050 on a practice that already has services: the way down and up again."""

from tests.clients_helpers import sql
from tests.domain_factories import practice
from tests.migration_helpers import head_revision
from tests.test_service_task_templates import _service, _set
from tests.test_ticket_migration import migrate


async def test_the_table_goes_with_the_way_down_and_comes_back_empty(client) -> None:
    owner, org = await practice(client)
    service = await _service(client, owner, org)
    await _set(client, owner, org, service, [{"title": "Prepare"}, {"title": "Review"}])
    try:
        down = await migrate("downgrade", "049")
        assert down.returncode == 0, down.stderr
        assert (await sql("SELECT to_regclass('public.service_task_templates')::text AS t"))[0][
            "t"
        ] is None
        # The services themselves are untouched.
        assert len(await sql("SELECT 1 FROM services WHERE id = :s", s=service["id"])) == 1
    finally:
        up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert (await sql("SELECT version_num FROM alembic_version"))[0][
        "version_num"
    ] == head_revision()
    assert await sql("SELECT 1 FROM service_task_templates") == []
