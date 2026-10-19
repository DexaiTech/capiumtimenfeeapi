"""Expense lines: the amounts (net, VAT, gross), mileage, the markup and the recharge amount, the
billable rules, clients and jobs, dates and types, and how an edit recalculates a line."""

from tests.billing_helpers import count_rows, set_org
from tests.clients_helpers import sql
from tests.domain_factories import make_client, make_job, practice, vat_rate_id
from tests.expense_helpers import (
    add_line,
    claim_url,
    create_claim,
    expense_team,
    line_body,
    patch_line,
    type_id,
)
from tests.factories import audit_entries

LINE_KEYS = {
    "id",
    "claim_id",
    "expense_type_id",
    "expense_type",
    "incurred_on",
    "description",
    "client_id",
    "client",
    "job_id",
    "job",
    "supplier",
    "vat_rate_id",
    "vat_rate_bp",
    "net",
    "vat",
    "gross",
    "miles",
    "mileage_rate_applied",
    "is_billable",
    "markup_bp",
    "recharge_amount",
    "payer",
    "billing_status",
    "receipt_required",
    "receipt_count",
    "receipts",
    "created_at",
    "updated_at",
}


def money(line: dict) -> tuple[int, int, int]:
    return line["net"], line["vat"], line["gross"]


async def draft(client, team):
    """A practice's draft claim of its Owner."""
    return await create_claim(client, team.owner, team.org)


# --- Amounts --------------------------------------------------------------------------------


async def test_a_line_has_the_documented_shape_and_snapshots_the_vat_code(client):
    team = await expense_team(client)
    claim = await draft(client, team)
    acme = await make_client(team.org, "Acme Ltd")
    line = await add_line(
        client,
        team.owner,
        team.org,
        claim["id"],
        "ACCOM",
        client_id=str(acme.id),
        supplier="Premier Inn",
        payer="practice",
    )
    assert set(line) == LINE_KEYS
    standard = await vat_rate_id(team.org, "standard")
    assert money(line) == (1000, 200, 1200)
    assert (line["vat_rate_id"], line["vat_rate_bp"]) == (str(standard), 2000)
    assert line["expense_type"]["code"] == "ACCOM" and line["expense_type"]["is_mileage"] is False
    assert line["client"]["id"] == str(acme.id) and line["client"]["name"] == "Acme Ltd"
    assert line["job"] is None and line["miles"] is None and line["mileage_rate_applied"] is None
    assert line["supplier"] == "Premier Inn" and line["payer"] == "practice"
    assert line["billing_status"] == "unbilled" and line["claim_id"] == claim["id"]
    assert line["receipts"] == [] and line["receipt_count"] == 0
    # The snapshot survives a later change of the rate itself.
    await client.patch(
        team.url(f"/vat-rates/{standard}"), headers=team.owner.headers, json={"rate_bp": 2500}
    )
    detail = (await client.get(claim_url(team.org, claim["id"]), headers=team.owner.headers)).json()
    assert detail["data"]["lines"][0]["vat_rate_bp"] == 2000
    assert detail["data"]["lines"][0]["vat"] == 200


async def test_any_two_of_net_vat_and_gross_give_the_third(client):
    team = await expense_team(client)
    org = team.org
    claim = await draft(client, team)
    standard, reduced = await vat_rate_id(org, "standard"), await vat_rate_id(org, "reduced")
    zero = await vat_rate_id(org, "zero")
    # (type, request, (net, vat, gross), VAT code kept on the line)
    cases = [
        ("SUBSISTENCE", {"net": 1000}, (1000, 200, 1200), standard),  # the type's code
        ("TRAVEL", {"net": 1000}, (1000, 0, 1000), zero),
        ("OTHER", {"net": 1000, "vat_rate_id": str(reduced)}, (1000, 50, 1050), reduced),
        ("OTHER", {"net": 10, "vat_rate_id": str(reduced)}, (10, 1, 11), reduced),  # 0.5 up
        ("OTHER", {"net": 30, "vat_rate_id": str(reduced)}, (30, 2, 32), reduced),  # 1.5 up
        ("OTHER", {"net": 50, "vat_rate_id": str(reduced)}, (50, 3, 53), reduced),  # 2.5 up
        ("OTHER", {"net": 9, "vat_rate_id": str(reduced)}, (9, 0, 9), reduced),  # 0.45 down
        ("OTHER", {"net": None, "gross": 1200}, (1000, 200, 1200), standard),
        ("OTHER", {"net": None, "gross": 1000}, (833, 167, 1000), standard),  # 166.67 up
        (
            "OTHER",
            {"net": None, "gross": 1050, "vat_rate_id": str(reduced)},
            (1000, 50, 1050),
            reduced,
        ),
        ("OTHER", {"net": 1000, "vat": 150}, (1000, 150, 1150), None),
        ("OTHER", {"net": 1000, "gross": 1250}, (1000, 250, 1250), None),
        ("OTHER", {"net": None, "vat": 200, "gross": 1200}, (1000, 200, 1200), None),
        ("OTHER", {"net": 1000, "vat": 200, "gross": 1200}, (1000, 200, 1200), None),
        (
            "OTHER",
            {"net": 1000, "vat": 0, "vat_rate_id": str(zero)},
            (1000, 0, 1000),
            zero,
        ),
    ]
    for code, body, expected, rate in cases:
        line = await add_line(client, team.owner, org, claim["id"], code, **body)
        assert money(line) == expected, (code, body)
        assert line["gross"] == line["net"] + line["vat"]
        assert line["vat_rate_id"] == (str(rate) if rate else None), (code, body)
        assert (line["vat_rate_bp"] is None) == (rate is None)
    detail = (await client.get(claim_url(org, claim["id"]), headers=team.owner.headers)).json()
    totals = detail["data"]["totals"]
    assert totals["net"] == sum(line["net"] for line in detail["data"]["lines"])
    assert totals["gross"] == sum(c[2][2] for c in cases)
    assert totals["vat"] == totals["gross"] - totals["net"]


async def test_inconsistent_missing_or_impossible_amounts_are_refused(client):
    team = await expense_team(client)
    claim = await draft(client, team)
    cases = [
        ({"net": 1000, "vat": 200, "gross": 1000}, "gross"),
        ({"net": 1000, "gross": 900}, "gross"),
        ({"net": None, "vat": 500, "gross": 400}, "vat"),
        ({"net": None, "vat": 200}, "net"),
        ({"net": None}, "net"),
        ({"net": 0}, "net"),
        ({"net": 0, "vat": 0}, "net"),
        ({"net": -1}, "net"),
        ({"net": 1000, "vat": -1}, "vat"),
        ({"net": None, "gross": -5}, "gross"),
        ({"net": 10**12 + 1}, "net"),
        ({"net": 10.5}, "net"),
        ({"net": "1000x"}, "net"),
        ({"net": 1000, "gross": "abc"}, "gross"),
    ]
    for body, field in cases:
        error = await add_line(client, team.owner, team.org, claim["id"], status=422, **body)
        assert error["details"][0]["field"] == field, (body, error)
    assert await count_rows("SELECT count(*) FROM expenses") == 0
    assert await audit_entries(team.id, "expense.created") == []
    # A request that names a field the API does not know is refused too.
    response = await client.post(
        claim_url(team.org, claim["id"], "/lines"),
        headers=team.owner.headers,
        json={**await line_body(team.org), "cost": 5},
    )
    assert response.status_code == 422


# --- Mileage --------------------------------------------------------------------------------


async def test_mileage_is_priced_from_the_miles_and_the_practice_rate(client):
    team = await expense_team(client)
    org = team.org
    claim = await draft(client, team)
    first = await add_line(client, team.owner, org, claim["id"], "MILEAGE", net=None, miles=100)
    assert money(first) == (4500, 0, 4500)
    assert first["miles"] == "100.00" and first["mileage_rate_applied"] == 45
    assert first["vat_rate_bp"] == 0  # the type's code (outside the scope)
    half = await add_line(client, team.owner, org, claim["id"], "MILEAGE", net=None, miles="12.5")
    assert money(half) == (563, 0, 563) and half["miles"] == "12.50"  # 562.5 rounds up
    small = await add_line(client, team.owner, org, claim["id"], "MILEAGE", net=None, miles=0.03)
    assert money(small) == (1, 0, 1)  # 1.35 rounds down

    # A new rate applies to new lines only; each line keeps the rate it was priced at.
    await set_org(org, mileage_rate=60)
    later = await add_line(client, team.owner, org, claim["id"], "MILEAGE", net=None, miles=10)
    assert money(later) == (600, 0, 600) and later["mileage_rate_applied"] == 60
    detail = (await client.get(claim_url(org, claim["id"]), headers=team.owner.headers)).json()
    kept = {line["id"]: line for line in detail["data"]["lines"]}
    assert money(kept[first["id"]]) == (4500, 0, 4500)
    assert kept[first["id"]]["mileage_rate_applied"] == 45

    # VAT can be given explicitly, like any line.
    vatted = await add_line(
        client, team.owner, org, claim["id"], "MILEAGE", net=None, miles=10, vat=60
    )
    assert money(vatted) == (600, 60, 660)


async def test_mileage_rules(client):
    team = await expense_team(client)
    org = team.org
    claim = await draft(client, team)

    cases = [
        ({"net": 500, "miles": 10}, "net", "mileage_priced"),
        ({"net": None, "gross": 500, "miles": 10}, "gross", "mileage_priced"),
        ({"net": None}, "miles", "required"),
        ({"net": None, "miles": 0}, "miles", None),
        ({"net": None, "miles": -3}, "miles", None),
        ({"net": None, "miles": "1000000"}, "miles", None),  # more than 999,999.99
        ({"net": None, "miles": "1.234"}, "miles", None),  # three decimals
        ({"net": None, "miles": "abc"}, "miles", None),
        ({"net": None, "miles": 0.01}, "net", "not_positive"),  # prices to nothing
    ]
    for body, field, code in cases:
        error = await add_line(client, team.owner, org, claim["id"], "MILEAGE", status=422, **body)
        assert error["details"][0]["field"] == field, (body, error)
        if code:
            assert error["details"][0]["code"] == code, (body, error)
    # Other types refuse miles.
    error = await add_line(client, team.owner, org, claim["id"], "TRAVEL", status=422, miles=10)
    assert error["details"][0]["field"] == "miles" and error["details"][0]["code"] == "not_mileage"
    assert await count_rows("SELECT count(*) FROM expenses") == 0
    ok = await add_line(
        client, team.owner, org, claim["id"], "MILEAGE", net=None, miles="999999.99"
    )
    assert money(ok) == (round(999_999.99 * 45), 0, round(999_999.99 * 45))


async def test_editing_mileage(client):
    team = await expense_team(client)
    org = team.org
    claim = await draft(client, team)
    line = await add_line(client, team.owner, org, claim["id"], "MILEAGE", net=None, miles=100)
    await set_org(org, mileage_rate=60)
    # An edit that leaves the miles alone keeps the price and the rate it was priced at.
    same = await patch_line(
        client, team.owner, org, claim["id"], line["id"], description="Visit to Leeds"
    )
    assert money(same) == (4500, 0, 4500) and same["mileage_rate_applied"] == 45
    # New miles are priced at the rate of the day.
    more = await patch_line(client, team.owner, org, claim["id"], line["id"], miles=50)
    assert money(more) == (3000, 0, 3000) and more["mileage_rate_applied"] == 60
    assert more["miles"] == "50.00"
    # The net cannot be typed in for a mileage line.
    error = await patch_line(client, team.owner, org, claim["id"], line["id"], status=422, net=100)
    assert error["details"][0]["field"] == "net"
    # Becoming another type drops the miles and keeps the money; becoming a mileage type needs miles.
    travel = await type_id(org, "TRAVEL")
    plain = await patch_line(
        client, team.owner, org, claim["id"], line["id"], expense_type_id=str(travel)
    )
    assert plain["miles"] is None and plain["mileage_rate_applied"] is None
    assert money(plain) == (3000, 0, 3000)
    mileage = await type_id(org, "MILEAGE")
    needs = await patch_line(
        client, team.owner, org, claim["id"], line["id"], status=422, expense_type_id=str(mileage)
    )
    assert needs["details"][0]["field"] == "miles"
    back = await patch_line(
        client, team.owner, org, claim["id"], line["id"], expense_type_id=str(mileage), miles=20
    )
    assert money(back) == (1200, 0, 1200) and back["mileage_rate_applied"] == 60


# --- Markup and recharge --------------------------------------------------------------------


async def test_a_billable_line_is_recharged_at_the_markup_rounded_half_up(client):
    team = await expense_team(client)
    org = team.org
    await set_org(org, expense_markup_bp=1250)
    acme = await make_client(org, "Acme Ltd")
    claim = await draft(client, team)
    for net, recharge in ((1000, 1125), (333, 375), (1, 1), (4, 5), (3999, 4499)):
        line = await add_line(
            client, team.owner, org, claim["id"], "TRAVEL", net=net, client_id=str(acme.id)
        )
        assert line["is_billable"] is True and line["markup_bp"] == 1250
        assert line["recharge_amount"] == recharge, net
    # No client, so not billable by default: nothing to recharge.
    internal = await add_line(client, team.owner, org, claim["id"], "TRAVEL")
    assert internal["is_billable"] is False and internal["recharge_amount"] == 0
    detail = (await client.get(claim_url(org, claim["id"]), headers=team.owner.headers)).json()
    assert detail["data"]["totals"]["recharge"] == 1125 + 375 + 1 + 5 + 4499


async def test_the_markup_is_kept_on_the_line_until_the_line_is_priced_again(client):
    team = await expense_team(client)
    org = team.org
    await set_org(org, expense_markup_bp=1000)
    acme = await make_client(org, "Acme Ltd")
    claim = await draft(client, team)
    line = await add_line(
        client, team.owner, org, claim["id"], "TRAVEL", net=2000, client_id=str(acme.id)
    )
    assert (line["markup_bp"], line["recharge_amount"]) == (1000, 2200)
    await set_org(org, expense_markup_bp=0)
    same = await patch_line(client, team.owner, org, claim["id"], line["id"], description="Taxi")
    assert (same["markup_bp"], same["recharge_amount"]) == (1000, 2200)
    # A new net prices the recharge again, with today's markup.
    again = await patch_line(client, team.owner, org, claim["id"], line["id"], net=3000)
    assert (again["markup_bp"], again["recharge_amount"]) == (0, 3000)
    # So does switching billable off and on.
    off = await patch_line(client, team.owner, org, claim["id"], line["id"], is_billable=False)
    assert (off["is_billable"], off["recharge_amount"]) == (False, 0)
    on = await patch_line(client, team.owner, org, claim["id"], line["id"], is_billable=True)
    assert on["recharge_amount"] == 3000


async def test_the_recharge_can_be_overridden_by_expense_manage_only(client):
    team = await expense_team(client)
    org = team.org
    await set_org(org, expense_markup_bp=1000)
    acme = await make_client(org, "Acme Ltd")
    staff = await team.member("staff")
    mine = await create_claim(client, staff, org)
    theirs = await draft(client, team)

    line = await add_line(
        client, team.owner, org, theirs["id"], "TRAVEL", client_id=str(acme.id), recharge_amount=700
    )
    assert (line["recharge_amount"], line["markup_bp"]) == (700, 1000)
    # Own claim without expense.manage: 403 whatever the value, nothing written.
    denied = await add_line(
        client,
        staff,
        org,
        mine["id"],
        "TRAVEL",
        status=403,
        client_id=str(acme.id),
        recharge_amount=5,
    )
    assert denied["code"] == "PERMISSION_DENIED"
    plain = await add_line(client, staff, org, mine["id"], "TRAVEL", client_id=str(acme.id))
    assert plain["recharge_amount"] == 1100  # the staff member's line still gets the default
    denied = await patch_line(
        client, staff, org, mine["id"], plain["id"], status=403, recharge_amount=None
    )
    assert denied["code"] == "PERMISSION_DENIED"

    # A new net prices the override away; null asks for the default again.
    repriced = await patch_line(client, team.owner, org, theirs["id"], line["id"], net=2000)
    assert repriced["recharge_amount"] == 2200
    set_again = await patch_line(
        client, team.owner, org, theirs["id"], line["id"], recharge_amount=1500
    )
    assert set_again["recharge_amount"] == 1500
    reset = await patch_line(
        client, team.owner, org, theirs["id"], line["id"], recharge_amount=None
    )
    assert reset["recharge_amount"] == 2200
    # Zero is a valid override; a non-billable line has nothing to recharge.
    zero = await patch_line(client, team.owner, org, theirs["id"], line["id"], recharge_amount=0)
    assert zero["recharge_amount"] == 0
    internal = await add_line(
        client, team.owner, org, theirs["id"], "TRAVEL", status=422, recharge_amount=5
    )
    assert internal["details"][0]["field"] == "recharge_amount"
    free = await add_line(client, team.owner, org, theirs["id"], "TRAVEL", recharge_amount=0)
    assert free["recharge_amount"] == 0


# --- Billable -------------------------------------------------------------------------------


async def test_billable_defaults_and_rules(client):
    team = await expense_team(client)
    org = team.org
    acme = await make_client(org, "Acme Ltd")
    claim = await draft(client, team)
    cid = str(acme.id)
    # The type's default, when there is a client.
    assert (await add_line(client, team.owner, org, claim["id"], "TRAVEL", client_id=cid))[
        "is_billable"
    ] is True
    assert (await add_line(client, team.owner, org, claim["id"], "SUBSISTENCE", client_id=cid))[
        "is_billable"
    ] is False
    assert (await add_line(client, team.owner, org, claim["id"], "TRAVEL"))["is_billable"] is False
    # Explicit choices.
    forced = await add_line(
        client, team.owner, org, claim["id"], "SUBSISTENCE", client_id=cid, is_billable=True
    )
    assert forced["is_billable"] is True and forced["recharge_amount"] == 1000
    declined = await add_line(
        client, team.owner, org, claim["id"], "TRAVEL", client_id=cid, is_billable=False
    )
    assert declined["is_billable"] is False and declined["recharge_amount"] == 0
    # Billable needs a client; a non-billable job's expenses cannot be billable.
    error = await add_line(
        client, team.owner, org, claim["id"], "TRAVEL", status=422, is_billable=True
    )
    assert error["details"][0]["field"] == "is_billable"
    internal = await make_job(org, acme, "Internal", billing_basis="non_billable")
    error = await add_line(
        client,
        team.owner,
        org,
        claim["id"],
        "TRAVEL",
        status=422,
        client_id=cid,
        job_id=str(internal.id),
        is_billable=True,
    )
    assert error["details"][0]["field"] == "is_billable"
    default = await add_line(
        client, team.owner, org, claim["id"], "TRAVEL", client_id=cid, job_id=str(internal.id)
    )
    assert default["is_billable"] is False
    # Taking the client away takes the billable flag with it.
    line = await add_line(client, team.owner, org, claim["id"], "TRAVEL", client_id=cid)
    assert line["is_billable"] is True
    detached = await patch_line(client, team.owner, org, claim["id"], line["id"], client_id=None)
    assert (detached["client_id"], detached["is_billable"], detached["recharge_amount"]) == (
        None,
        False,
        0,
    )
    error = await patch_line(
        client, team.owner, org, claim["id"], line["id"], status=422, is_billable=True
    )
    assert error["details"][0]["field"] == "is_billable"


# --- Clients, jobs, dates and types ---------------------------------------------------------


async def test_client_and_job_rules(client):
    team = await expense_team(client)
    org = team.org
    acme = await make_client(org, "Acme Ltd")
    other = await make_client(org, "Other Ltd")
    job = await make_job(org, acme, "Accounts")
    claim = await draft(client, team)

    line = await add_line(
        client, team.owner, org, claim["id"], client_id=str(acme.id), job_id=str(job.id)
    )
    assert line["job"]["id"] == str(job.id) and line["job"]["name"] == "Accounts"
    # A job alone gives its client.
    derived = await add_line(client, team.owner, org, claim["id"], job_id=str(job.id))
    assert derived["client_id"] == str(acme.id)
    # The job must be the client's; unknown jobs and clients are not found.
    mismatch = await add_line(
        client,
        team.owner,
        org,
        claim["id"],
        status=422,
        client_id=str(other.id),
        job_id=str(job.id),
    )
    assert mismatch["details"][0]["field"] == "job_id"
    missing = "00000000-0000-4000-8000-000000000000"
    assert (await add_line(client, team.owner, org, claim["id"], status=404, job_id=missing))[
        "details"
    ] == {"field": "job_id"}
    assert (await add_line(client, team.owner, org, claim["id"], status=404, client_id=missing))[
        "code"
    ] == "RESOURCE_NOT_FOUND"
    # Another practice's client and job are not found either.
    _, foreign_org = await practice(client, "Other Practice")
    foreign_client = await make_client(foreign_org, "Foreign Ltd")
    foreign_job = await make_job(foreign_org, foreign_client, "Foreign job")
    await add_line(
        client, team.owner, org, claim["id"], status=404, client_id=str(foreign_client.id)
    )
    await add_line(client, team.owner, org, claim["id"], status=404, job_id=str(foreign_job.id))

    # Moving to another client clears the job unless one is given.
    moved = await patch_line(
        client, team.owner, org, claim["id"], line["id"], client_id=str(other.id)
    )
    assert (moved["client_id"], moved["job_id"]) == (str(other.id), None)
    other_job = await make_job(org, other, "Tax")
    both = await patch_line(
        client,
        team.owner,
        org,
        claim["id"],
        line["id"],
        client_id=str(acme.id),
        job_id=str(job.id),
    )
    assert (both["client_id"], both["job_id"]) == (str(acme.id), str(job.id))
    wrong = await patch_line(
        client, team.owner, org, claim["id"], line["id"], status=422, job_id=str(other_job.id)
    )
    assert wrong["details"][0]["field"] == "job_id"


async def test_a_closed_job_and_an_inactive_client_refuse_new_expenses(client):
    team = await expense_team(client)
    org = team.org
    acme = await make_client(org, "Acme Ltd")
    claim = await draft(client, team)
    for status in ("completed", "cancelled"):
        closed = await make_job(org, acme, f"{status} job", status=status)
        error = await add_line(
            client, team.owner, org, claim["id"], status=409, job_id=str(closed.id)
        )
        assert error["code"] == "JOB_CLOSED" and error["details"]["job_id"] == str(closed.id)
    for status in ("planned", "in_progress", "on_hold"):
        job = await make_job(org, acme, f"{status} job", status=status)
        await add_line(client, team.owner, org, claim["id"], job_id=str(job.id))
    for status in ("inactive", "archived"):
        dormant = await make_client(org, f"{status} Ltd", status=status)
        error = await add_line(
            client, team.owner, org, claim["id"], status=409, client_id=str(dormant.id)
        )
        assert error["code"] == "CLIENT_INACTIVE"
    prospect = await make_client(org, "Prospect Ltd", status="prospect")
    await add_line(client, team.owner, org, claim["id"], client_id=str(prospect.id))

    # A line already on a job that closes later can still be edited; moving it onto a closed job cannot.
    open_job = await make_job(org, acme, "Open job")
    line = await add_line(client, team.owner, org, claim["id"], job_id=str(open_job.id))
    await sql("UPDATE jobs SET status = 'completed' WHERE id = :i", i=open_job.id)
    edited = await patch_line(
        client, team.owner, org, claim["id"], line["id"], description="Still editable"
    )
    assert edited["description"] == "Still editable" and edited["job_id"] == str(open_job.id)
    spare = await make_job(org, acme, "Spare job")
    await patch_line(client, team.owner, org, claim["id"], line["id"], job_id=str(spare.id))
    again = await patch_line(
        client, team.owner, org, claim["id"], line["id"], status=409, job_id=str(open_job.id)
    )
    assert again["code"] == "JOB_CLOSED"
