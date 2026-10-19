"""Expense lines: dates, descriptions and types, the VAT code, how an edit recalculates a line,
the audit of line edits and the limit of lines per claim."""

from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from tests.billing_helpers import count_rows
from tests.clients_helpers import sql
from tests.domain_factories import practice, vat_rate_id
from tests.expense_helpers import (
    add_line,
    claim_url,
    create_claim,
    expense_team,
    patch_line,
    type_id,
)
from tests.factories import audit_entries


def money(line: dict) -> tuple[int, int, int]:
    return line["net"], line["vat"], line["gross"]


async def draft(client, team):
    """A practice's draft claim of its Owner."""
    return await create_claim(client, team.owner, team.org)


async def test_date_description_supplier_and_payer_rules(client):
    team = await expense_team(client)
    org = team.org
    claim = await draft(client, team)
    soon = (date.today() + timedelta(days=3)).isoformat()
    error = await add_line(client, team.owner, org, claim["id"], status=422, incurred_on=soon)
    assert error["details"][0]["field"] == "incurred_on"
    assert error["details"][0]["code"] == "future_date"
    for bad in ("not-a-date", "2026-13-40", 20260907):
        await add_line(client, team.owner, org, claim["id"], status=422, incurred_on=bad)
    # The practice's today is allowed; so is any day before it.
    today = datetime.now(ZoneInfo(org["timezone"])).date()
    line = await add_line(client, team.owner, org, claim["id"], incurred_on=str(today))
    assert line["incurred_on"] == str(today)
    assert (await add_line(client, team.owner, org, claim["id"], incurred_on="2019-01-31"))["id"]

    for description in ("", "   ", "x" * 501):
        await add_line(client, team.owner, org, claim["id"], status=422, description=description)
    assert (await add_line(client, team.owner, org, claim["id"], description="y" * 500))["id"]
    assert (await add_line(client, team.owner, org, claim["id"], supplier="   "))[
        "supplier"
    ] is None
    await add_line(client, team.owner, org, claim["id"], status=422, supplier="s" * 201)
    await add_line(client, team.owner, org, claim["id"], status=422, payer="the boss")
    assert (await add_line(client, team.owner, org, claim["id"]))["payer"] == "staff"
    practice_paid = await add_line(client, team.owner, org, claim["id"], payer="practice")
    assert practice_paid["payer"] == "practice"
    detail = (await client.get(claim_url(org, claim["id"]), headers=team.owner.headers)).json()
    totals = detail["data"]["totals"]
    # Only what the staff member paid is reimbursed.
    assert totals["reimbursable"] == totals["gross"] - 1000


async def test_expense_type_rules(client):
    team = await expense_team(client)
    org = team.org
    claim = await draft(client, team)
    missing = "00000000-0000-4000-8000-000000000000"
    refused = await add_line(
        client, team.owner, org, claim["id"], status=404, expense_type_id=missing
    )
    assert refused["details"] == {"field": "expense_type_id"}
    # Another practice's type is not found either.
    other_org = (await practice(client, "Other Practice"))[1]
    foreign = await type_id(other_org, "TRAVEL")
    await add_line(client, team.owner, org, claim["id"], status=404, expense_type_id=str(foreign))

    software = await type_id(org, "SOFTWARE")
    line = await add_line(client, team.owner, org, claim["id"], "SOFTWARE")
    off = await client.patch(
        team.url(f"/expense-types/{software}"),
        headers=team.owner.headers,
        json={"is_active": False},
    )
    assert off.status_code == 200
    # An inactive type takes no new lines and no moved lines; its existing lines stay editable.
    inactive = await add_line(client, team.owner, org, claim["id"], "SOFTWARE", status=422)
    assert inactive["details"][0]["field"] == "expense_type_id"
    edited = await patch_line(
        client, team.owner, org, claim["id"], line["id"], description="Licence"
    )
    assert edited["description"] == "Licence"
    other_line = await add_line(client, team.owner, org, claim["id"], "OTHER")
    error = await patch_line(
        client,
        team.owner,
        org,
        claim["id"],
        other_line["id"],
        status=422,
        expense_type_id=str(software),
    )
    assert error["details"][0]["field"] == "expense_type_id"


async def test_the_vat_code_must_be_a_live_rate_of_the_practice(client):
    team = await expense_team(client)
    org = team.org
    other_org = (await practice(client, "Other Practice"))[1]
    foreign = await vat_rate_id(other_org)
    archived = await vat_rate_id(org, "reduced")
    await sql("UPDATE vat_rates SET archived_at = now() WHERE id = :i", i=archived)
    claim = await draft(client, team)
    for rate in (foreign, archived, "00000000-0000-4000-8000-000000000000"):
        error = await add_line(
            client, team.owner, org, claim["id"], status=422, vat_rate_id=str(rate)
        )
        assert error["details"][0]["field"] == "vat_rate_id"


# --- Editing --------------------------------------------------------------------------------


async def test_editing_a_line_recalculates_only_what_it_must(client):
    team = await expense_team(client)
    org = team.org
    claim = await draft(client, team)
    standard = await vat_rate_id(org, "standard")
    reduced = await vat_rate_id(org, "reduced")
    line = await add_line(client, team.owner, org, claim["id"], "OTHER")  # net 1000, 20% VAT
    assert money(line) == (1000, 200, 1200)
    cid, lid = claim["id"], line["id"]

    # Nothing about money in the request: the money stays.
    kept = await patch_line(client, team.owner, org, cid, lid, description="Stamps", supplier="PO")
    assert money(kept) == (1000, 200, 1200) and kept["supplier"] == "PO"
    # Net alone prices the VAT again from the line's code.
    net = await patch_line(client, team.owner, org, cid, lid, net=2000)
    assert money(net) == (2000, 400, 2400)
    # VAT alone keeps the net.
    vat = await patch_line(client, team.owner, org, cid, lid, vat=350)
    assert money(vat) == (2000, 350, 2350)
    # Gross alone derives the net and the VAT from the code.
    gross = await patch_line(client, team.owner, org, cid, lid, gross=1200)
    assert money(gross) == (1000, 200, 1200)
    # Net and gross give the VAT.
    both = await patch_line(client, team.owner, org, cid, lid, net=1000, gross=1100)
    assert money(both) == (1000, 100, 1100)
    # A new VAT code alone prices the VAT on the same net.
    code = await patch_line(client, team.owner, org, cid, lid, vat_rate_id=str(reduced))
    assert money(code) == (1000, 50, 1050)
    assert (code["vat_rate_id"], code["vat_rate_bp"]) == (str(reduced), 500)
    # Removing the code keeps the amounts and the snapshot goes with it.
    none = await patch_line(client, team.owner, org, cid, lid, vat_rate_id=None)
    assert money(none) == (1000, 50, 1050)
    assert (none["vat_rate_id"], none["vat_rate_bp"]) == (None, None)
    back = await patch_line(client, team.owner, org, cid, lid, vat_rate_id=str(standard), net=500)
    assert money(back) == (500, 100, 600)

    for body, field in (
        ({"net": 100, "vat": 5, "gross": 90}, "gross"),
        ({"gross": 0}, "net"),
        ({"incurred_on": (date.today() + timedelta(days=4)).isoformat()}, "incurred_on"),
        ({"description": ""}, "description"),
    ):
        error = await patch_line(client, team.owner, org, cid, lid, status=422, **body)
        assert error["details"][0]["field"] == field, body
    for body in ({"net": None}, {"description": None}, {"payer": None}, {"incurred_on": None}):
        await patch_line(client, team.owner, org, cid, lid, status=422, **body)


async def test_line_edits_are_audited_with_their_changes_and_an_unchanged_edit_is_not(client):
    team = await expense_team(client)
    org = team.org
    claim = await draft(client, team)
    line = await add_line(client, team.owner, org, claim["id"], "OTHER", description="Stamps")
    cid, lid = claim["id"], line["id"]
    changed = await patch_line(client, team.owner, org, cid, lid, net=2000, supplier="Post Office")
    assert changed["net"] == 2000
    # The same values again change nothing and leave no audit row.
    await patch_line(client, team.owner, org, cid, lid, net=2000, supplier="Post Office")
    await patch_line(client, team.owner, org, cid, lid, description="Stamps")
    deleted = await client.delete(claim_url(org, cid, f"/lines/{lid}"), headers=team.owner.headers)
    assert deleted.status_code == 200 and deleted.json()["data"] is None

    actions = Counter(
        e["action"] for e in await audit_entries(team.id) if e["target_type"] == "expense"
    )
    assert actions == {"expense.created": 1, "expense.updated": 1, "expense.deleted": 1}
    [entry] = await audit_entries(team.id, "expense.updated")
    assert entry["target_id"] == lid and entry["actor_user_id"] == str(team.owner.id)
    meta = entry["metadata"]
    assert meta["changes"] == {
        "net": [1000, 2000],
        "vat": [200, 400],
        "gross": [1200, 2400],
        "supplier": [None, "Post Office"],
    }
    assert meta["claim_id"] == cid and meta["description"] == "Stamps" and meta["gross"] == 2400
    [created] = await audit_entries(team.id, "expense.created")
    assert created["metadata"]["net"] == 1000 and created["metadata"]["payer"] == "staff"
    [removed] = await audit_entries(team.id, "expense.deleted")
    assert removed["metadata"]["gross"] == 2400 and removed["target_id"] == lid
    rows = await sql(
        "SELECT ip_address::text AS ip, request_id FROM audit_logs WHERE action LIKE 'expense.%'"
    )
    assert len(rows) == 3 and all(r["ip"] and r["request_id"] for r in rows)


async def test_a_claim_holds_a_limited_number_of_lines(client, monkeypatch):
    team = await expense_team(client)
    monkeypatch.setattr("app.services.expense_claims.MAX_LINES_PER_CLAIM", 2)
    claim = await draft(client, team)
    await add_line(client, team.owner, team.org, claim["id"])
    await add_line(client, team.owner, team.org, claim["id"])
    error = await add_line(client, team.owner, team.org, claim["id"], status=409)
    assert error["code"] == "EXPENSE_CLAIM_LINE_LIMIT"
    assert await count_rows("SELECT count(*) FROM expenses") == 2
