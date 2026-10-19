"""Migrations 016-018 and the constraints they put in the database."""

import pytest
from sqlalchemy.exc import IntegrityError

from tests.central_helpers import BAKERY, PLUMBING, enabled, new_team, sql
from tests.clients_helpers import create_client, org_url
from tests.migration_helpers import head_revision, migrate

NEW_TABLES = (
    "central_client_cache",
    "integration_sync_logs",
    "integration_cursors",
    "invoice_templates",
)
NEW_CLIENT_COLUMNS = {
    "central_client_id",
    "central_synced_at",
    "central_sync_state",
    "merged_into_client_id",
    "invoice_email",
    "default_service_id",
    "invoice_template_id",
}
AUDIT_INDEXES = {
    "ix_audit_logs_organization_id_client_target_created_at",
    "ix_audit_logs_organization_id_client_ref_created_at",
}


async def exists(table: str) -> bool:
    rows = await sql("SELECT to_regclass(:t)::text AS t", t=f"public.{table}")
    return rows[0]["t"] is not None


# --- Existing practices ---------------------------------------------------------------------------


async def test_existing_practices_get_the_standard_template_and_keep_their_clients(client):
    first = await new_team(client)
    second = await new_team(client, "Second Practice")
    acme = await create_client(client, first.owner, first.org, "Acme Ltd", email="hi@acme.example")
    try:
        down = await migrate("downgrade", "015")
        assert down.returncode == 0, down.stderr
        # Everything of 016-018 is gone ...
        for table in NEW_TABLES:
            assert not await exists(table), table
        columns = {
            row["column_name"]
            for row in await sql(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'clients'"
            )
        }
        assert not columns & NEW_CLIENT_COLUMNS
        indexes = {
            row["indexname"]
            for row in await sql("SELECT indexname FROM pg_indexes WHERE tablename = 'audit_logs'")
        }
        assert not indexes & AUDIT_INDEXES
        assert (await sql("SELECT version_num FROM alembic_version")) == [{"version_num": "015"}]
        # ... and the practices and their clients are untouched.
        assert len(await sql("SELECT 1 FROM organizations")) == 2
        assert [r["name"] for r in await sql("SELECT name FROM clients")] == ["Acme Ltd"]
    finally:
        up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert (await sql("SELECT version_num FROM alembic_version")) == [
        {"version_num": head_revision()}
    ]
    for table in NEW_TABLES:
        assert await exists(table), table

    # Every practice has exactly one template again: the default, called Standard.
    for team in (first, second):
        templates = await sql(
            "SELECT name, layout, show_logo, show_bank_details, footer_text, is_default, status, "
            "deleted_at FROM invoice_templates WHERE organization_id = :o",
            o=team.id,
        )
        assert templates == [
            {
                "name": "Standard",
                "layout": "standard",
                "show_logo": True,
                "show_bank_details": True,
                "footer_text": None,
                "is_default": True,
                "status": "active",
                "deleted_at": None,
            }
        ]
    # The client that existed before is a local client with nothing set.
    [row] = await sql(
        "SELECT name, email, central_client_id, central_sync_state, central_synced_at, "
        "merged_into_client_id, invoice_email, default_service_id, invoice_template_id "
        "FROM clients WHERE id = :i",
        i=acme["id"],
    )
    assert row == {
        "name": "Acme Ltd",
        "email": "hi@acme.example",
        "central_client_id": None,
        "central_sync_state": "local",
        "central_synced_at": None,
        "merged_into_client_id": None,
        "invoice_email": None,
        "default_service_id": None,
        "invoice_template_id": None,
    }

    # The practices can use all of it straight away.
    listed = await client.get(org_url(first.org, "/invoice-templates"), headers=first.owner.headers)
    assert [t["name"] for t in listed.json()["data"]] == ["Standard"]
    enabled_client = await enabled(client, first.owner, first.org, BAKERY)
    assert enabled_client["client"]["central_client_id"] == BAKERY
    overview = await client.get(
        org_url(first.org, f"/clients/{acme['id']}/overview"), headers=first.owner.headers
    )
    assert overview.status_code == 200

    # Models and migrations agree, and running the migrations again changes nothing.
    check = await migrate("check")
    assert check.returncode == 0, check.stdout + check.stderr
    assert "No new upgrade operations detected" in check.stdout + check.stderr
    again = await migrate("upgrade", "head")
    assert again.returncode == 0, again.stderr


# --- Constraints ----------------------------------------------------------------------------------


async def two_clients(client):
    team = await new_team(client)
    one = await create_client(client, team.owner, team.org, "One Ltd")
    two = await create_client(client, team.owner, team.org, "Two Ltd")
    return team, one["id"], two["id"]


@pytest.mark.parametrize(
    ("assignments", "constraint"),
    [
        (
            "central_client_id = 'CC-1', central_sync_state = 'bogus'",
            "ck_clients_central_sync_state",
        ),
        ("central_client_id = 'CC-1'", "ck_clients_central_state_matches_link"),
        ("central_sync_state = 'synced'", "ck_clients_central_state_matches_link"),
        (
            "central_client_id = '', central_sync_state = 'synced'",
            "ck_clients_central_client_id_length",
        ),
        (
            "central_client_id = repeat('x', 65), central_sync_state = 'synced'",
            "ck_clients_central_client_id_length",
        ),
        ("merged_into_client_id = id", "ck_clients_not_merged_into_itself"),
        ("invoice_email = 'ab'", "ck_clients_invoice_email_length"),
        ("invoice_email = repeat('a', 255)", "ck_clients_invoice_email_length"),
        (
            "default_service_id = gen_random_uuid()",
            "fk_clients_organization_id_default_service_id_services",
        ),
        (
            "invoice_template_id = gen_random_uuid()",
            "fk_clients_invoice_template_invoice_templates",
        ),
        (
            "merged_into_client_id = gen_random_uuid()",
            "fk_clients_organization_id_merged_into_client_id_clients",
        ),
    ],
)
async def test_the_clients_constraints(client, assignments, constraint):
    _, one, _ = await two_clients(client)
    with pytest.raises(IntegrityError, match=constraint):
        await sql(f"UPDATE clients SET {assignments} WHERE id = :i", i=one)  # noqa: S608


async def test_a_central_client_is_linked_to_one_live_client_per_practice(client):
    _, one, two = await two_clients(client)
    link = (
        "UPDATE clients SET central_client_id = 'CC-1', central_sync_state = 'synced' WHERE id = :i"
    )
    await sql(link, i=one)
    with pytest.raises(IntegrityError, match="uq_clients_organization_id_central_client_id"):
        await sql(link, i=two)
    # A deleted client gives the link back; another practice has its own clients.
    await sql("UPDATE clients SET deleted_at = now() WHERE id = :i", i=one)
    await sql(link, i=two)
    other = await new_team(client, "Second Practice")
    stranger = await create_client(client, other.owner, other.org, "Stranger Ltd")
    await sql(link, i=stranger["id"])


async def test_a_client_cannot_be_merged_into_another_practices_client(client):
    _, one, _ = await two_clients(client)
    other = await new_team(client, "Second Practice")
    stranger = await create_client(client, other.owner, other.org, "Stranger Ltd")
    with pytest.raises(
        IntegrityError, match="fk_clients_organization_id_merged_into_client_id_clients"
    ):
        await sql(
            "UPDATE clients SET merged_into_client_id = :m WHERE id = :i", m=stranger["id"], i=one
        )


async def test_the_sync_log_constraints(client):
    team = await new_team(client)

    async def insert(
        *, integration="central_client", status="ok", event=None, direction="inbound", attempt=1
    ):
        await sql(
            "INSERT INTO integration_sync_logs (id, organization_id, integration, direction, "
            "operation, status, external_event_id, attempt) VALUES (gen_random_uuid(), :o, "
            ":integration, :direction, 'sync', :status, :event, :attempt)",
            o=team.id,
            integration=integration,
            direction=direction,
            status=status,
            event=event,
            attempt=attempt,
        )

    await insert(event="evt-1")
    with pytest.raises(IntegrityError, match="uq_integration_sync_logs_event"):
        await insert(event="evt-1")
    # Another integration and rows without an event id are not duplicates.
    await insert(event="evt-1", integration="central_user")
    await insert()
    await insert()
    with pytest.raises(IntegrityError, match="ck_integration_sync_logs_status"):
        await insert(status="maybe")
    with pytest.raises(IntegrityError, match="ck_integration_sync_logs_integration_format"):
        await insert(integration="Central Client")
    with pytest.raises(IntegrityError, match="ck_integration_sync_logs_direction"):
        await insert(direction="sideways")
    with pytest.raises(IntegrityError, match="ck_integration_sync_logs_attempt"):
        await insert(attempt=0)
    # Another practice has its own log: the same event id is a different event there.
    other = await new_team(client, "Second Practice")
    await sql(
        "INSERT INTO integration_sync_logs (id, organization_id, integration, operation, status, "
        "external_event_id) VALUES (gen_random_uuid(), :o, 'central_client', 'sync', 'ok', 'evt-1')",
        o=other.id,
    )


async def test_the_invoice_template_constraints(client):
    team = await new_team(client)
    insert = (
        "INSERT INTO invoice_templates (id, organization_id, name, layout, is_default, status, footer_text, deleted_at) "
        "VALUES (gen_random_uuid(), :o, :name, :layout, :default, :status, :footer, :deleted)"
    )

    async def add(
        name="Detailed",
        layout="detailed",
        default=False,
        status="active",
        footer=None,
        deleted=None,
    ):
        await sql(
            insert,
            o=team.id,
            name=name,
            layout=layout,
            default=default,
            status=status,
            footer=footer,
            deleted=deleted,
        )

    await add()
    with pytest.raises(IntegrityError, match="uq_invoice_templates_organization_id_name_lower"):
        await add(name="DETAILED")
    with pytest.raises(IntegrityError, match="uq_invoice_templates_organization_id_default"):
        await add(name="Second default", default=True)
    with pytest.raises(IntegrityError, match="ck_invoice_templates_layout"):
        await add(name="Odd", layout="fancy")
    with pytest.raises(IntegrityError, match="ck_invoice_templates_status"):
        await add(name="Odd", status="archived")
    with pytest.raises(IntegrityError, match="ck_invoice_templates_default_is_active"):
        await sql(
            "UPDATE invoice_templates SET status = 'inactive' WHERE organization_id = :o AND is_default",
            o=team.id,
        )
    with pytest.raises(IntegrityError, match="ck_invoice_templates_footer_text_length"):
        await add(name="Long footer", footer="x" * 1001)
    with pytest.raises(IntegrityError, match="ck_invoice_templates_footer_text_length"):
        await add(name="Empty footer", footer="")
    with pytest.raises(IntegrityError, match="ck_invoice_templates_name_length"):
        await add(name="")
    # A deleted template frees its name and may keep the default flag.
    await sql("UPDATE invoice_templates SET deleted_at = now() WHERE name = 'Detailed'")
    await add(name="detailed")
    rows = await sql(
        "SELECT updated_at >= created_at AS ok FROM invoice_templates WHERE name = 'detailed'"
    )
    assert rows == [{"ok": True}]


async def test_a_central_cache_row_is_unique_per_practice_and_central_client(client):
    team = await new_team(client)
    insert = (
        "INSERT INTO central_client_cache (id, organization_id, central_id, name, status, source_updated_at) "
        "VALUES (gen_random_uuid(), :o, :c, :n, :s, now())"
    )
    await sql(insert, o=team.id, c=BAKERY, n="Northgate Bakery Ltd", s="active")
    with pytest.raises(IntegrityError, match="uq_central_client_cache_organization_id_central_id"):
        await sql(insert, o=team.id, c=BAKERY, n="Northgate Again", s="active")
    with pytest.raises(IntegrityError, match="ck_central_client_cache_status"):
        await sql(insert, o=team.id, c=PLUMBING, n="Brightwater", s="gone")
    with pytest.raises(IntegrityError, match="ck_central_client_cache_name_length"):
        await sql(insert, o=team.id, c=PLUMBING, n="", s="active")
    other = await new_team(client, "Second Practice")
    await sql(insert, o=other.id, c=BAKERY, n="Northgate Bakery Ltd", s="active")
