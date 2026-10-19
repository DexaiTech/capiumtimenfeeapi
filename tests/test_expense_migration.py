"""Migrations 024 to 026 on practices that already exist (the way down and up again), models and
migrations agreeing for the expense tables, and the rules the database itself enforces: money that
adds up, the claim's status fields, one practice never touching another's rows."""

import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db import models  # noqa: F401  (registers every table on Base.metadata)
from app.db.base import Base
from app.db.models.expenses import MAX_RECEIPT_BYTES
from tests.billing_helpers import count_rows
from tests.clients_helpers import sql
from tests.domain_factories import make_client, make_job
from tests.expense_helpers import (
    create_claim,
    expense_team,
    make_claim,
    make_line,
    type_id,
    upload_receipt,
)
from tests.migration_helpers import head_revision, migrate

EXPENSE_TABLES = (
    "expense_types",
    "expense_claims",
    "expenses",
    "expense_claim_events",
    "expense_receipts",
)
# code -> (name, the code of the default VAT rate, mileage, billable by default)
DEFAULT_TYPES = {
    "TRAVEL": ("Travel", "zero", False, True),
    "MILEAGE": ("Mileage", "outside_scope", True, True),
    "SUBSISTENCE": ("Subsistence", "standard", False, False),
    "ACCOM": ("Accommodation", "standard", False, True),
    "POSTAGE": ("Postage", "exempt", False, True),
    "PRINTING": ("Printing", "standard", False, True),
    "SOFTWARE": ("Software", "standard", False, False),
    "OTHER": ("Other", "standard", False, False),
}


async def types_of(org_id: str) -> dict[str, tuple]:
    rows = await sql(
        "SELECT t.code, t.name, r.code AS vat, t.is_mileage, t.default_billable, t.is_active "
        "FROM expense_types t LEFT JOIN vat_rates r "
        "ON r.organization_id = t.organization_id AND r.id = t.default_vat_rate_id "
        "WHERE t.organization_id = :o",
        o=org_id,
    )
    return {
        row["code"]: (
            row["name"],
            row["vat"],
            row["is_mileage"],
            row["default_billable"],
            row["is_active"],
        )
        for row in rows
    }


def default_types(**changes: tuple) -> dict[str, tuple]:
    expected = {
        code: (name, vat, mileage, billable, True)
        for code, (name, vat, mileage, billable) in DEFAULT_TYPES.items()
    }
    expected.update(changes)
    return expected


# --- The migrations on existing practices ---------------------------------------------------


async def test_existing_practices_get_the_default_expense_types_and_the_tables_come_and_go(client):
    first = await expense_team(client)
    second = await expense_team(client, "Second Practice")
    staff = await first.member("staff")
    claim = await create_claim(client, staff, first.org, lines=1)
    assert (
        await upload_receipt(client, staff, first.org, claim["lines"][0]["id"])
    ).status_code == 201
    # The second practice has retired its zero rate: Travel has no default VAT code afterwards.
    await sql(
        "UPDATE vat_rates SET archived_at = now() WHERE organization_id = :o AND code = 'zero'",
        o=second.id,
    )
    assert await types_of(first.id) == default_types()

    try:
        down = await migrate("downgrade", "023")
        assert down.returncode == 0, down.stderr
        # Everything of 024 to 026 is gone; the practices, their people and permissions are not.
        for table in EXPENSE_TABLES:
            assert (await sql(f"SELECT to_regclass('public.{table}')::text AS t"))[0]["t"] is None
        assert (await sql("SELECT version_num FROM alembic_version")) == [{"version_num": "023"}]
        assert len(await sql("SELECT 1 FROM organizations")) == 2
        codes = await sql("SELECT code FROM permissions WHERE code LIKE 'expense.%'")
        assert {row["code"] for row in codes} == {
            "expense.log",
            "expense.read_all",
            "expense.approve",
            "expense.manage",
        }
    finally:
        up = await migrate("upgrade", "head")
    assert up.returncode == 0, up.stderr

    assert await types_of(first.id) == default_types()
    assert await types_of(second.id) == default_types(TRAVEL=("Travel", None, False, True, True))
    assert (await sql("SELECT version_num FROM alembic_version"))[0][
        "version_num"
    ] == head_revision()
    # What the downgrade dropped is gone for good.
    for table in ("expense_claims", "expenses", "expense_claim_events", "expense_receipts"):
        assert await count_rows(f"SELECT count(*) FROM {table}") == 0  # noqa: S608

    # The practices use expenses straight away, with the types the migration made.
    for team, vat_code in ((first, "zero"), (second, None)):
        made = await create_claim(client, team.owner, team.org, lines=1)
        line = made["lines"][0]
        assert line["expense_type"]["code"] == "TRAVEL" and line["gross"] == 1000
        assert (line["vat_rate_id"] is None) is (vat_code is None)
    # Running the migration again changes nothing.
    again = await migrate("upgrade", "head")
    assert again.returncode == 0, again.stderr
    assert await count_rows("SELECT count(*) FROM expense_types") == 16


def _only_expense_tables(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table":
        return name in EXPENSE_TABLES
    table = getattr(obj, "table", None)
    return table is not None and table.name in EXPENSE_TABLES


def _drift(connection) -> list:
    context = MigrationContext.configure(connection, opts={"include_object": _only_expense_tables})
    return compare_metadata(context, Base.metadata)


async def test_the_models_and_the_migrations_agree_for_the_expense_tables(client):
    engine = create_async_engine(get_settings().direct_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            drift = await connection.run_sync(_drift)
    finally:
        await engine.dispose()
    assert drift == []


# --- What the database enforces -------------------------------------------------------------


async def refused(statement: str, **params) -> None:
    with pytest.raises(IntegrityError):
        await sql(statement, **params)


async def test_the_database_refuses_lines_that_do_not_add_up_or_break_the_billing_rules(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    acme = await make_client(org, "Acme Ltd")
    beta = await make_client(org, "Beta Ltd")
    beta_job = await make_job(org, beta, "Beta job")
    claim = await make_claim(org, staff)
    line = await make_line(org, claim, client=acme, net=1000, vat=200)
    plain = await make_line(org, claim)
    standard = (
        await sql(
            "SELECT id FROM vat_rates WHERE organization_id = :o AND code = 'standard'", o=team.id
        )
    )[0]["id"]

    for statement, params in (
        ("UPDATE expenses SET gross = gross + 1 WHERE id = :i", {"i": line}),
        ("UPDATE expenses SET net = -1, gross = -1 WHERE id = :i", {"i": plain}),
        ("UPDATE expenses SET vat = -1, gross = 999 WHERE id = :i", {"i": plain}),
        ("UPDATE expenses SET description = '' WHERE id = :i", {"i": plain}),
        ("UPDATE expenses SET description = repeat('x', 501) WHERE id = :i", {"i": plain}),
        ("UPDATE expenses SET supplier = '' WHERE id = :i", {"i": plain}),
        ("UPDATE expenses SET payer = 'bank' WHERE id = :i", {"i": plain}),
        ("UPDATE expenses SET markup_bp = 100001 WHERE id = :i", {"i": line}),
        # A line is billable only with a client, and only a billable line has a recharge.
        ("UPDATE expenses SET is_billable = true WHERE id = :i", {"i": plain}),
        ("UPDATE expenses SET recharge_amount = 5 WHERE id = :i", {"i": line}),
        ("UPDATE expenses SET recharge_amount = -1, is_billable = true WHERE id = :i", {"i": line}),
        ("UPDATE expenses SET job_id = :j WHERE id = :i", {"i": plain, "j": beta_job.id}),
        # Only a billable line can be billed.
        ("UPDATE expenses SET billing_status = 'billed' WHERE id = :i", {"i": line}),
        (
            "UPDATE expenses SET billing_status = 'paid', is_billable = true WHERE id = :i",
            {"i": line},
        ),
        # The VAT code and its rate are kept together; so are the miles and their rate.
        ("UPDATE expenses SET vat_rate_id = :r WHERE id = :i", {"i": line, "r": standard}),
        ("UPDATE expenses SET vat_rate_bp = 2000 WHERE id = :i", {"i": line}),
        (
            "UPDATE expenses SET vat_rate_id = :r, vat_rate_bp = 10001 WHERE id = :i",
            {"i": line, "r": standard},
        ),
        ("UPDATE expenses SET miles = 10 WHERE id = :i", {"i": line}),
        ("UPDATE expenses SET mileage_rate_applied = 45 WHERE id = :i", {"i": line}),
        ("UPDATE expenses SET miles = 0, mileage_rate_applied = 45 WHERE id = :i", {"i": line}),
    ):
        await refused(statement, **params)
    # A job must be a job of the line's own client.
    await refused(
        "UPDATE expenses SET client_id = :c, job_id = :j WHERE id = :i",
        i=line,
        c=acme.id,
        j=beta_job.id,
    )
    # Nothing changed, and consistent edits are fine.
    assert (await sql("SELECT net, vat, gross FROM expenses WHERE id = :i", i=line))[0] == {
        "net": 1000,
        "vat": 200,
        "gross": 1200,
    }
    await sql("UPDATE expenses SET net = 2000, vat = 400, gross = 2400 WHERE id = :i", i=line)
    await sql(
        "UPDATE expenses SET is_billable = true, recharge_amount = 500, billing_status = 'billed' "
        "WHERE id = :i",
        i=line,
    )
    await sql(
        "UPDATE expenses SET miles = 10.5, mileage_rate_applied = 45, vat_rate_id = :r, "
        "vat_rate_bp = 2000 WHERE id = :i",
        i=line,
        r=standard,
    )


async def test_the_database_refuses_claims_in_an_impossible_state(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    draft = await make_claim(org, staff)
    submitted = await make_claim(org, staff, status="submitted")
    approved = await make_claim(org, staff, status="approved", decided_by=team.owner)
    for statement, params in (
        # The columns of each status.
        ("UPDATE expense_claims SET status = 'submitted' WHERE id = :c", {"c": draft}),
        ("UPDATE expense_claims SET submitted_at = now() WHERE id = :c", {"c": draft}),
        ("UPDATE expense_claims SET status = 'approved' WHERE id = :c", {"c": submitted}),
        ("UPDATE expense_claims SET submitted_at = NULL WHERE id = :c", {"c": submitted}),
        ("UPDATE expense_claims SET decided_by = NULL WHERE id = :c", {"c": approved}),
        ("UPDATE expense_claims SET decided_at = NULL WHERE id = :c", {"c": approved}),
        ("UPDATE expense_claims SET status = 'draft' WHERE id = :c", {"c": approved}),
        ("UPDATE expense_claims SET status = 'paid' WHERE id = :c", {"c": approved}),
        # A returned claim says why; notes and titles have limits.
        (
            "UPDATE expense_claims SET status = 'returned', decision_note = NULL WHERE id = :c",
            {"c": approved},
        ),
        ("UPDATE expense_claims SET decision_note = '' WHERE id = :c", {"c": approved}),
        (
            "UPDATE expense_claims SET decision_note = repeat('x', 2001) WHERE id = :c",
            {"c": approved},
        ),
        ("UPDATE expense_claims SET title = '' WHERE id = :c", {"c": draft}),
        ("UPDATE expense_claims SET title = repeat('x', 201) WHERE id = :c", {"c": draft}),
        # Nobody outside the practice may be the claimant, its creator or its decider.
        ("UPDATE expense_claims SET user_id = gen_random_uuid() WHERE id = :c", {"c": draft}),
        ("UPDATE expense_claims SET created_by = gen_random_uuid() WHERE id = :c", {"c": draft}),
        ("UPDATE expense_claims SET decided_by = gen_random_uuid() WHERE id = :c", {"c": approved}),
    ):
        await refused(statement, **params)
    assert [
        r["status"] for r in await sql("SELECT status FROM expense_claims ORDER BY status")
    ] == [
        "approved",
        "draft",
        "submitted",
    ]


async def test_one_practice_cannot_reach_the_rows_of_another(client):
    team = await expense_team(client)
    other = await expense_team(client, "Other Practice")
    staff, their_staff = await team.member("staff"), await other.member("staff")
    mine = await make_claim(team.org, staff)
    my_line = await make_line(team.org, mine)
    their_client = await make_client(other.org, "Their Ltd")
    their_claim = await make_claim(other.org, their_staff)
    their_line = await make_line(other.org, their_claim)
    their_type = await type_id(other.org, "OTHER")
    for statement, params in (
        ("UPDATE expenses SET claim_id = :x WHERE id = :i", {"i": my_line, "x": their_claim}),
        ("UPDATE expenses SET client_id = :x WHERE id = :i", {"i": my_line, "x": their_client.id}),
        ("UPDATE expenses SET expense_type_id = :x WHERE id = :i", {"i": my_line, "x": their_type}),
        (
            "UPDATE expenses SET vat_rate_id = (SELECT id FROM vat_rates WHERE organization_id = :o "
            "LIMIT 1), vat_rate_bp = 0 WHERE id = :i",
            {"i": my_line, "o": other.id},
        ),
        ("UPDATE expense_claims SET user_id = :x WHERE id = :i", {"i": mine, "x": their_staff.id}),
        (
            "INSERT INTO expense_claim_events (id, organization_id, claim_id, action, actor_user_id) "
            "VALUES (gen_random_uuid(), :o, :c, 'submitted', :u)",
            {"o": team.id, "c": their_claim, "u": staff.id},
        ),
        (
            "INSERT INTO expense_receipts (id, organization_id, expense_id, file_name, content_type, "
            "size_bytes, storage_key, sha256, uploaded_by) VALUES (gen_random_uuid(), :o, :e, 'r.png', "
            "'image/png', 10, :k, :s, :u)",
            {"o": team.id, "e": their_line, "k": "k" * 20, "s": "a" * 64, "u": staff.id},
        ),
    ):
        await refused(statement, **params)
    # The default VAT code of a type is a VAT code of the same practice.
    await refused(
        "UPDATE expense_types SET default_vat_rate_id = (SELECT id FROM vat_rates "
        "WHERE organization_id = :o LIMIT 1) WHERE organization_id = :mine AND code = 'OTHER'",
        o=other.id,
        mine=team.id,
    )


async def test_expense_types_are_unique_by_code_and_by_active_name(client):
    team = await expense_team(client)
    insert = (
        "INSERT INTO expense_types (id, organization_id, name, code, is_active) "
        "VALUES (gen_random_uuid(), :o, :n, :c, :a)"
    )
    # The same code twice, the same name twice while both are active (whatever the case).
    await refused(insert, o=team.id, n="Another", c="TRAVEL", a=True)
    await refused(insert, o=team.id, n="travel", c="T2", a=True)
    await refused(insert, o=team.id, n="", c="T2", a=True)
    await refused(insert, o=team.id, n="x" * 81, c="T2", a=True)
    for code in ("lower", "WAY_TOO_LONG_CODE", "BAD CODE", "BAD/CODE", ""):
        await refused(insert, o=team.id, n="Something", c=code, a=True)
    # A retired type may share a name; bringing it back is refused while the name is taken.
    await sql(insert, o=team.id, n="Travel", c="OLDTRAVEL", a=False)
    await refused("UPDATE expense_types SET is_active = true WHERE code = 'OLDTRAVEL'")
    # Another practice may use the same names and codes.
    other = await expense_team(client, "Other Practice")
    assert len(await types_of(other.id)) == 8
    await sql(insert, o=other.id, n="Courier", c="COURIER", a=True)
    # A type that lines use cannot be deleted.
    staff = await team.member("staff")
    await make_line(team.org, await make_claim(team.org, staff))
    await refused(
        "DELETE FROM expense_types WHERE organization_id = :o AND code = 'TRAVEL'", o=team.id
    )


async def test_receipts_and_history_rows_obey_their_limits_and_go_with_their_claim(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    claim = await make_claim(org, staff)
    line = await make_line(org, claim)
    insert_receipt = (
        "INSERT INTO expense_receipts (id, organization_id, expense_id, file_name, content_type, "
        "size_bytes, storage_key, sha256, uploaded_by) VALUES (gen_random_uuid(), :o, :e, :n, :t, "
        ":size, :k, :s, :u)"
    )
    good = {
        "o": team.id,
        "e": line,
        "n": "r.png",
        "t": "image/png",
        "size": 10,
        "k": "k" * 20,
        "s": "a" * 64,
        "u": staff.id,
    }
    for change in (
        {"size": 0},
        {"size": MAX_RECEIPT_BYTES + 1},
        {"size": -5},
        {"k": "short"},
        {"k": "k" * 129},
        {"s": "A" * 64},
        {"s": "a" * 63},
        {"n": ""},
        {"n": "n" * 201},
        {"t": "x"},
        {"u": uuid.uuid4()},
    ):
        await refused(insert_receipt, **{**good, **change})
    await sql(insert_receipt, **{**good, "size": MAX_RECEIPT_BYTES})
    await refused(insert_receipt, **{**good, "n": "again.png"})  # the key is unique
    insert_event = (
        "INSERT INTO expense_claim_events (id, organization_id, claim_id, action, actor_user_id, note) "
        "VALUES (gen_random_uuid(), :o, :c, :a, :u, :n)"
    )
    event = {"o": team.id, "c": claim, "a": "submitted", "u": staff.id, "n": None}
    for change in ({"a": "paid"}, {"n": ""}, {"n": "n" * 2001}, {"u": uuid.uuid4()}):
        await refused(insert_event, **{**event, **change})
    for action in ("submitted", "recalled", "approved", "returned", "corrected"):
        await sql(insert_event, **{**event, "a": action, "n": "ok"})

    # Deleting the claim takes its lines, receipts and history with it.
    await sql("DELETE FROM expense_claims WHERE id = :c", c=claim)
    for table in ("expenses", "expense_receipts", "expense_claim_events"):
        assert await count_rows(f"SELECT count(*) FROM {table}") == 0  # noqa: S608


async def test_updated_at_moves_with_every_change(client):
    team = await expense_team(client)
    staff = await team.member("staff")
    claim = await make_claim(team.org, staff)
    line = await make_line(team.org, claim)
    for table, row_id, change in (
        ("expense_claims", claim, "title = 'Edited'"),
        ("expenses", line, "description = 'Edited'"),
        ("expense_types", None, "name = 'Edited'"),
    ):
        where = "id = :i" if row_id else "organization_id = :o AND code = 'OTHER'"
        await sql(f"UPDATE {table} SET {change} WHERE {where}", i=row_id, o=team.id)  # noqa: S608
        [row] = await sql(
            f"SELECT updated_at > created_at AS moved FROM {table} WHERE {where}",  # noqa: S608
            i=row_id,
            o=team.id,
        )
        assert row["moved"] is True, table
