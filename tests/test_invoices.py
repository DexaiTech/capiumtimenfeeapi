"""Drafting invoices from WIP: grouping, overrides and apportionment, fixed fees, lines,
VAT resolution and rounding."""

import asyncio
from datetime import date

from sqlalchemy import text

from app.db.session import get_sessionmaker
from tests.billing_helpers import (
    billing_status,
    count_rows,
    draft,
    get_invoice,
    hourly_job,
    new_practice,
    org_url,
)
from tests.domain_factories import (
    make_client,
    make_disbursement,
    make_job,
    make_time_entry,
    service_id,
    staff,
    vat_rate_id,
)
from tests.factories import audit_entries, error


async def allocations(invoice_id: str) -> dict[str, tuple[int, int]]:
    """item id -> (wip_value, billed_value) of the invoice's live allocations."""
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            text(
                "SELECT coalesce(time_entry_id, disbursement_id), wip_value, billed_value "
                "FROM wip_allocations WHERE invoice_id = :id AND reversed_at IS NULL"
            ),
            {"id": invoice_id},
        )
        return {str(r[0]): (r[1], r[2]) for r in rows}


async def test_draft_one_line_per_job(client):
    p = await new_practice(client)
    accounts = await hourly_job(p, name="Accounts 2026")
    payroll = await hourly_job(p, name="Payroll")
    a1 = await make_time_entry(
        p.org, p.owner, client=p.client, job=accounts, minutes=60, entry_date=date(2026, 9, 1)
    )
    a2 = await make_time_entry(
        p.org,
        p.owner,
        client=p.client,
        job=accounts,
        minutes=30,
        charge_rate=15_000,
        entry_date=date(2026, 9, 14),
    )
    b1 = await make_time_entry(
        p.org, p.owner, client=p.client, job=payroll, minutes=45, charge_rate=10_000
    )
    loose = await make_time_entry(p.org, p.owner, client=p.client, minutes=6)
    fee = await make_disbursement(p.org, p.client, 1_300, supplier="Companies House")

    invoice = await draft(
        client, p.owner, p.org, p.client.id, time=[a1, a2, b1, loose], disbursements=[fee]
    )
    assert invoice["status"] == "draft" and invoice["number"] is None
    assert invoice["display_status"] == "draft"
    lines = invoice["lines"]
    assert [line["kind"] for line in lines] == ["time", "time", "time", "disbursement"]
    first = lines[0]
    assert first["description"] == "Accounts 2026 (1 Sep 2026 to 14 Sep 2026)"
    assert first["quantity"] == "1.50"
    # 12,000 + 7,500 over 1.5 hours: an average of £130/h.
    assert (first["unit_amount"], first["net_amount"]) == (13_000, 19_500)
    assert (first["vat_rate_bp"], first["vat_amount"]) == (2000, 3_900)
    assert first["wip"]["item_count"] == 2 and first["wip"]["adjustment"] == 0
    assert lines[1]["description"].startswith("Payroll (")
    assert lines[1]["net_amount"] == 7_500
    assert lines[2]["description"].startswith("Professional services (")
    assert (lines[2]["quantity"], lines[2]["net_amount"]) == ("0.10", 1_200)
    assert lines[3]["description"] == "Companies House filing fee (Companies House)"
    assert lines[3]["net_amount"] == 1_300 and lines[3]["vat_amount"] == 260

    net = 19_500 + 7_500 + 1_200 + 1_300
    assert invoice["net_total"] == net
    assert invoice["vat_total"] == 3_900 + 1_500 + 240 + 260
    assert invoice["gross_total"] == invoice["net_total"] + invoice["vat_total"]
    assert invoice["wip"] == {"wip_value": net, "billed_value": net, "adjustment": 0}
    assert set((await billing_status("time_entries", [a1.id, a2.id, b1.id])).values()) == {
        "in_draft"
    }
    assert (await billing_status("disbursements", [fee.id]))[fee.id] == "in_draft"
    [entry] = await audit_entries(p.id, "invoice.draft_created")
    assert entry["metadata"]["client_name"] == "Widgets Ltd"
    assert entry["metadata"]["gross_total"] == invoice["gross_total"]


async def test_group_by_entry_and_single(client):
    p = await new_practice(client)
    job = await hourly_job(p, name="Advice")
    e1 = await make_time_entry(
        p.org, p.owner, client=p.client, job=job, minutes=60, description="Call with director"
    )
    e2 = await make_time_entry(
        p.org, p.owner, client=p.client, minutes=30, entry_date=date(2026, 9, 9)
    )
    by_entry = await draft(client, p.owner, p.org, p.client.id, time=[e1, e2], group_by="entry")
    assert [line["description"] for line in by_entry["lines"]] == [
        "7 Sep 2026: Call with director",
        "9 Sep 2026: Professional services",
    ]
    assert [line["unit_amount"] for line in by_entry["lines"]] == [12_000, 12_000]
    await client.delete(org_url(p.org, f"/invoices/{by_entry['id']}"), headers=p.owner.headers)

    single = await draft(client, p.owner, p.org, p.client.id, time=[e1, e2], group_by="single")
    [line] = single["lines"]
    assert line["description"] == "Professional services (7 Sep 2026 to 9 Sep 2026)"
    assert (line["quantity"], line["net_amount"]) == ("1.50", 18_000)
    assert line["wip"]["item_count"] == 2


async def test_override_write_off_is_apportioned_exactly(client):
    p = await new_practice(client)
    job = await hourly_job(p)
    big = await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=60)
    small = await make_time_entry(
        p.org, p.owner, client=p.client, job=job, minutes=30, charge_rate=15_000
    )
    invoice = await draft(
        client,
        p.owner,
        p.org,
        p.client.id,
        time=[big, small],
        overrides=[{"job_id": str(job.id), "net_amount": 18_000, "description": "Bookkeeping"}],
    )
    [line] = invoice["lines"]
    assert (line["net_amount"], line["description"]) == (18_000, "Bookkeeping")
    assert line["unit_amount"] == 12_000  # 18,000 over 1.5 hours
    assert line["vat_amount"] == 3_600
    assert line["wip"] == {
        "item_count": 2,
        "time_entry_ids": line["wip"]["time_entry_ids"],
        "disbursement_ids": [],
        "wip_value": 19_500,
        "billed_value": 18_000,
        "adjustment": -1_500,
    }
    # 18,000 x 12,000/19,500 = 11,076.92 and x 7,500/19,500 = 6,923.08: largest remainder.
    assert await allocations(invoice["id"]) == {
        str(big.id): (12_000, 11_077),
        str(small.id): (7_500, 6_923),
    }


async def test_override_write_up_and_by_item(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client, minutes=20)
    fee = await make_disbursement(p.org, p.client, 1_000)
    zero = await vat_rate_id(p.org, "zero")
    invoice = await draft(
        client,
        p.owner,
        p.org,
        p.client.id,
        time=[entry],
        disbursements=[fee],
        overrides=[
            {"time_entry_id": str(entry.id), "net_amount": 5_000},
            {"disbursement_id": str(fee.id), "vat_rate_id": str(zero)},
        ],
    )
    time_line, disbursement_line = invoice["lines"]
    assert time_line["wip"]["adjustment"] == 5_000 - 4_000
    assert (disbursement_line["vat_rate_bp"], disbursement_line["vat_amount"]) == (0, 0)


async def test_override_errors(client):
    p = await new_practice(client)
    job = await hourly_job(p)
    e1 = await make_time_entry(p.org, p.owner, client=p.client, job=job)
    e2 = await make_time_entry(p.org, p.owner, client=p.client, job=job)
    stranger = await make_time_entry(p.org, p.owner, client=p.client)

    async def attempt(overrides, **body):
        return await draft(
            client,
            p.owner,
            p.org,
            p.client.id,
            time=[e1, e2],
            overrides=overrides,
            expect=422,
            **body,
        )

    assert (await attempt([{"time_entry_id": str(stranger.id), "net_amount": 1}]))["code"] == (
        "OVERRIDE_NO_LINE"
    )
    ambiguous = await attempt([{"job_id": str(job.id), "net_amount": 1}], group_by="entry")
    assert ambiguous["code"] == "OVERRIDE_AMBIGUOUS"
    twice = await attempt(
        [
            {"time_entry_id": str(e1.id), "net_amount": 1},
            {"time_entry_id": str(e2.id), "net_amount": 2},
        ]
    )
    assert twice["code"] == "OVERRIDE_DUPLICATE"
    assert (await attempt([{"net_amount": 1}]))["code"] == "VALIDATION_ERROR"
    assert (await attempt([{"job_id": str(job.id), "net_amount": -1}]))["code"] == (
        "VALIDATION_ERROR"
    )
    # Nothing was left behind by the failed drafts.
    assert await count_rows("SELECT count(*) FROM invoices") == 0
    assert set((await billing_status("time_entries", [e1.id, e2.id])).values()) == {"unbilled"}


async def test_fixed_fee_job_bills_the_fee_once(client):
    p = await new_practice(client)
    accounts_service = await service_id(p.org, "ACCS")
    job = await make_job(
        p.org,
        p.client,
        "Year-end accounts 2026",
        billing_basis="fixed",
        fixed_fee=120_000,
        service_id=accounts_service,
    )
    t1 = await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=300)
    t2 = await make_time_entry(
        p.org, p.owner, client=p.client, job=job, minutes=200, charge_rate=9_000
    )
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[t1, t2], group_by="entry")
    [line] = invoice["lines"]
    assert line["kind"] == "fixed_fee"
    assert line["description"] == "Year-end accounts 2026 (fixed fee)"
    assert (line["quantity"], line["unit_amount"], line["net_amount"]) == ("1.00", 120_000, 120_000)
    assert line["service_id"] == str(accounts_service)
    wip = 60_000 + 30_000
    assert line["wip"]["wip_value"] == wip and line["wip"]["billed_value"] == 120_000
    billed = await allocations(invoice["id"])
    assert sum(b for _, b in billed.values()) == 120_000
    assert billed[str(t1.id)] == (60_000, 80_000)

    # More time on the same job, on another draft: the fee was billed already.
    t3 = await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=60)
    second = await draft(client, p.owner, p.org, p.client.id, time=[t3])
    assert second["lines"][0]["kind"] == "fixed_fee"
    assert second["lines"][0]["net_amount"] == 0


async def test_racing_drafts_for_one_fixed_fee_job_bill_the_fee_once(client):
    """Two drafts for different time of the same fixed-fee job, at the same moment: the
    fee is billed once in total (the second waits for the first and bills what is left)."""
    p = await new_practice(client)
    for _ in range(3):
        job = await make_job(
            p.org, p.client, "Year-end accounts", billing_basis="fixed", fixed_fee=50_000
        )
        entries = [
            await make_time_entry(p.org, p.owner, client=p.client, job=job) for _ in range(4)
        ]

        async def attempt(entry):
            return await client.post(
                p.url("/invoices"),
                headers=p.owner.headers,
                json={"client_id": str(p.client.id), "time_entry_ids": [str(entry.id)]},
            )

        results = await asyncio.gather(*(attempt(e) for e in entries))
        assert all(r.status_code == 201 for r in results), [r.text for r in results]
        fees = sorted(r.json()["data"]["lines"][0]["net_amount"] for r in results)
        assert fees == [0, 0, 0, 50_000]


async def test_adding_overridden_fixed_fee_time_to_its_line(client):
    p = await new_practice(client)
    job = await make_job(p.org, p.client, "Payroll", billing_basis="fixed", fixed_fee=10_000)
    f1 = await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=60)
    f2 = await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=60)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[f1])
    response = await client.post(
        org_url(p.org, f"/invoices/{invoice['id']}/wip"),
        headers=p.owner.headers,
        json={
            "time_entry_ids": [str(f2.id)],
            "overrides": [{"job_id": str(job.id), "net_amount": 9_001}],
        },
    )
    assert response.status_code == 200, response.text
    [line] = response.json()["data"]["lines"]
    assert (line["net_amount"], line["vat_amount"]) == (9_001, 1_800)
    assert line["wip"]["item_count"] == 2 and line["wip"]["billed_value"] == 9_001
    assert sorted(b for _, b in (await allocations(invoice["id"])).values()) == [4_500, 4_501]
    assert response.json()["data"]["net_total"] == 9_001


async def test_add_wip_to_a_draft(client):
    p = await new_practice(client)
    fixed = await make_job(p.org, p.client, "Payroll", billing_basis="fixed", fixed_fee=10_000)
    hourly = await hourly_job(p)
    f1 = await make_time_entry(p.org, p.owner, client=p.client, job=fixed, minutes=60)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[f1])
    f2 = await make_time_entry(p.org, p.owner, client=p.client, job=fixed, minutes=60)
    h1 = await make_time_entry(p.org, p.owner, client=p.client, job=hourly, minutes=30)
    fee = await make_disbursement(p.org, p.client, 2_000)
    response = await client.post(
        org_url(p.org, f"/invoices/{invoice['id']}/wip"),
        headers=p.owner.headers,
        json={"time_entry_ids": [str(f2.id), str(h1.id)], "disbursement_ids": [str(fee.id)]},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    kinds = [(line["kind"], line["position"]) for line in data["lines"]]
    assert kinds == [("fixed_fee", 1), ("time", 2), ("disbursement", 3)]
    fixed_line = data["lines"][0]
    assert fixed_line["net_amount"] == 10_000 and fixed_line["wip"]["item_count"] == 2
    assert data["net_total"] == 10_000 + 6_000 + 2_000
    empty = await client.post(
        org_url(p.org, f"/invoices/{invoice['id']}/wip"), headers=p.owner.headers, json={}
    )
    assert empty.status_code == 422


async def test_draft_item_rules(client):
    p = await new_practice(client)
    other_client = await make_client(p.org, "Other Ltd")
    mine = await make_time_entry(p.org, p.owner, client=p.client)
    theirs = await make_time_entry(p.org, p.owner, client=other_client)
    their_fee = await make_disbursement(p.org, other_client)
    unapproved = await make_time_entry(
        p.org, p.owner, client=p.client, entry_date=date(2026, 8, 3), timesheet_status="open"
    )
    non_billable = await make_time_entry(p.org, p.owner, client=p.client, is_billable=False)
    written_off = await make_time_entry(
        p.org, p.owner, client=p.client, billing_status="written_off"
    )
    other_practice = await new_practice(client, "Other Practice")
    foreign = await make_time_entry(
        other_practice.org, other_practice.owner, client=other_practice.client
    )

    for items in ([theirs], [mine, theirs]):
        response = await draft(client, p.owner, p.org, p.client.id, time=items, expect=422)
        assert response["code"] == "CLIENT_MISMATCH"
    mismatch = await draft(
        client, p.owner, p.org, p.client.id, disbursements=[their_fee], expect=422
    )
    assert mismatch["code"] == "CLIENT_MISMATCH"
    for item, reason in (
        (unapproved, "timesheet_not_approved"),
        (non_billable, "not_billable"),
        (written_off, "written_off"),
    ):
        response = await draft(client, p.owner, p.org, p.client.id, time=[item], expect=409)
        assert response["code"] == "WIP_NOT_AVAILABLE"
        assert response["details"][0]["reason"] == reason
    assert (await draft(client, p.owner, p.org, p.client.id, time=[foreign], expect=404))[
        "code"
    ] == "RESOURCE_NOT_FOUND"
    await draft(client, p.owner, p.org, other_practice.client.id, time=[mine], expect=404)
    bad_dates = await draft(
        client,
        p.owner,
        p.org,
        p.client.id,
        issue_date="2026-10-10",
        due_date="2026-10-01",
        expect=422,
    )
    assert bad_dates["details"][0]["field"] == "due_date"

    await draft(client, p.owner, p.org, p.client.id, time=[mine])
    again = await draft(client, p.owner, p.org, p.client.id, time=[mine], expect=409)
    assert again["details"][0]["reason"] == "in_draft"


async def test_inactive_clients_can_still_be_billed(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    async with get_sessionmaker()() as session:
        await session.execute(
            text("UPDATE clients SET status = 'archived' WHERE id = :id"), {"id": str(p.client.id)}
        )
        await session.commit()
    await draft(client, p.owner, p.org, p.client.id, time=[entry])


async def test_two_drafts_racing_for_the_same_time(client):
    p = await new_practice(client)
    entries = [await make_time_entry(p.org, p.owner, client=p.client) for _ in range(3)]

    async def attempt():
        return await client.post(
            p.url("/invoices"),
            headers=p.owner.headers,
            json={"client_id": str(p.client.id), "time_entry_ids": [str(e.id) for e in entries]},
        )

    results = await asyncio.gather(*(attempt() for _ in range(4)))
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 409, 409, 409]
    assert await count_rows("SELECT count(*) FROM invoices") == 1
    assert await count_rows("SELECT count(*) FROM wip_allocations") == 3


async def test_vat_rate_resolution_order(client):
    p = await new_practice(client)
    reduced = await vat_rate_id(p.org, "reduced")
    zero = await vat_rate_id(p.org, "zero")
    exempt = await vat_rate_id(p.org, "exempt")
    books = await service_id(p.org, "BOOKS")
    async with get_sessionmaker()() as session:
        await session.execute(
            text("UPDATE services SET vat_rate_id = :rate WHERE id = :id"),
            {"rate": str(reduced), "id": str(books)},
        )
        await session.commit()
    zero_client = await make_client(p.org, "Zero Ltd", default_vat_rate_id=zero)
    service_job = await make_job(p.org, zero_client, "Books", service_id=books)
    plain_job = await make_job(p.org, zero_client, "Ad hoc")
    on_service = await make_time_entry(p.org, p.owner, client=zero_client, job=service_job)
    on_client = await make_time_entry(p.org, p.owner, client=zero_client, job=plain_job)
    chosen = await make_disbursement(p.org, zero_client, 1_000, vat_rate_id=exempt)
    invoice = await draft(
        client,
        p.owner,
        p.org,
        zero_client.id,
        time=[on_service, on_client],
        disbursements=[chosen],
    )
    rates = {line["description"].split(" (")[0]: line for line in invoice["lines"]}
    assert rates["Books"]["vat_rate_bp"] == 500 and rates["Books"]["vat_rate_id"] == str(reduced)
    assert rates["Books"]["vat_amount"] == 600
    assert rates["Ad hoc"]["vat_rate_id"] == str(zero)
    assert invoice["lines"][2]["vat_rate_id"] == str(exempt)

    # A client without a default and a job without a service: the practice default (20%).
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    standard = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    assert standard["lines"][0]["vat_rate_bp"] == 2000
    # The line's own rate beats them all.
    line = await client.patch(
        org_url(p.org, f"/invoices/{standard['id']}/lines/{standard['lines'][0]['id']}"),
        headers=p.owner.headers,
        json={"vat_rate_id": str(reduced)},
    )
    assert line.json()["data"]["lines"][0]["vat_rate_bp"] == 500
    reset = await client.patch(
        org_url(p.org, f"/invoices/{standard['id']}/lines/{standard['lines'][0]['id']}"),
        headers=p.owner.headers,
        json={"vat_rate_id": None},
    )
    assert reset.json()["data"]["lines"][0]["vat_rate_bp"] == 2000


async def test_vat_is_rounded_per_line_half_up(client):
    p = await new_practice(client)
    invoice = await draft(client, p.owner, p.org, p.client.id)
    url = org_url(p.org, f"/invoices/{invoice['id']}/lines")
    reduced = await vat_rate_id(p.org, "reduced")
    for _ in range(3):
        # 20% of 3p is 0.6p -> 1p on each line (3p), not 20% of 9p = 1.8p -> 2p.
        await client.post(
            url, headers=p.owner.headers, json={"description": "Sundry", "unit_amount": 3}
        )
    # 5% of 10p is exactly 0.5p: half up gives 1p.
    response = await client.post(
        url,
        headers=p.owner.headers,
        json={"description": "Postage", "unit_amount": 10, "vat_rate_id": str(reduced)},
    )
    data = response.json()["data"]
    assert [line["vat_amount"] for line in data["lines"]] == [1, 1, 1, 1]
    assert (data["net_total"], data["vat_total"], data["gross_total"]) == (19, 4, 23)
    assert data["vat_summary"] == [
        {"vat_rate_bp": 2000, "net": 9, "vat": 3},
        {"vat_rate_bp": 500, "net": 10, "vat": 1},
    ]


async def test_non_vat_registered_practice_charges_no_vat(client):
    p = await new_practice(client, vat_registered=False)
    standard = await vat_rate_id(p.org, "standard")
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    fee = await make_disbursement(p.org, p.client, 1_300, vat_rate_id=standard)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry], disbursements=[fee])
    assert {(line["vat_rate_bp"], line["vat_amount"]) for line in invoice["lines"]} == {(0, 0)}
    assert invoice["gross_total"] == invoice["net_total"] == 13_300
    added = await client.post(
        org_url(p.org, f"/invoices/{invoice['id']}/lines"),
        headers=p.owner.headers,
        json={"description": "Extra", "unit_amount": 1_000, "vat_rate_id": str(standard)},
    )
    assert added.json()["data"]["vat_total"] == 0


async def test_manual_lines_and_discounts(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    url = org_url(p.org, f"/invoices/{invoice['id']}/lines")
    response = await client.post(
        url,
        headers=p.owner.headers,
        json={"description": "Report binding", "quantity": "2.5", "unit_amount": 1_999},
    )
    assert response.status_code == 201, response.text
    manual = response.json()["data"]["lines"][1]
    assert manual["kind"] == "manual" and manual["wip"] is None
    assert manual["net_amount"] == 4_998  # 4,997.5 rounded half up
    discount = await client.post(
        url,
        headers=p.owner.headers,
        json={"description": "Loyalty discount", "unit_amount": -2_000},
    )
    data = discount.json()["data"]
    assert data["net_total"] == 12_000 + 4_998 - 2_000
    assert data["lines"][2]["vat_amount"] == -400
    assert data["gross_total"] == data["net_total"] + data["vat_total"]
    too_much = await client.post(
        url,
        headers=p.owner.headers,
        json={"description": "Oops", "unit_amount": -100_000},
    )
    assert too_much.status_code == 422 and error(too_much)["code"] == "INVOICE_TOTAL_NEGATIVE"
    explicit = await client.post(
        url,
        headers=p.owner.headers,
        json={"description": "Fixed", "quantity": "3", "unit_amount": 100, "net_amount": 250},
    )
    assert explicit.json()["data"]["lines"][3]["net_amount"] == 250
    for bad in (
        {"description": "", "unit_amount": 1},
        {"description": "x", "unit_amount": 1, "quantity": "0"},
        {"description": "x", "unit_amount": 1, "quantity": "1.234"},
        {"description": "x"},
        {"description": "x", "unit_amount": 1, "kind": "time"},
    ):
        assert (await client.post(url, headers=p.owner.headers, json=bad)).status_code == 422


async def test_editing_a_wip_line_reapportions(client):
    p = await new_practice(client)
    job = await hourly_job(p)
    e1 = await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=60)
    e2 = await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=60)
    e3 = await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=60)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[e1, e2, e3])
    line = invoice["lines"][0]
    url = org_url(p.org, f"/invoices/{invoice['id']}/lines/{line['id']}")
    response = await client.patch(url, headers=p.owner.headers, json={"net_amount": 10_000})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["lines"][0]["net_amount"] == 10_000
    assert data["lines"][0]["wip"]["billed_value"] == 10_000
    assert data["lines"][0]["wip"]["adjustment"] == -26_000
    billed = sorted(b for _, b in (await allocations(invoice["id"])).values())
    assert billed == [3_333, 3_333, 3_334]
    assert data["net_total"] == 10_000 and data["vat_total"] == 2_000

    negative = await client.patch(url, headers=p.owner.headers, json={"net_amount": -1})
    assert negative.status_code == 422 and error(negative)["code"] == "NEGATIVE_WIP_LINE"
    renamed = await client.patch(
        url, headers=p.owner.headers, json={"description": "Monthly bookkeeping"}
    )
    assert renamed.json()["data"]["lines"][0]["description"] == "Monthly bookkeeping"
    assert renamed.json()["data"]["lines"][0]["net_amount"] == 10_000
    assert (
        await client.patch(url, headers=p.owner.headers, json={"net_amount": None})
    ).status_code == 422
    entry = (await audit_entries(p.id, "invoice.line_updated"))[0]
    assert entry["metadata"]["old"]["net_amount"] == 36_000
    assert entry["metadata"]["new"]["net_amount"] == 10_000


async def test_removing_lines_and_drafts_returns_wip(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    fee = await make_disbursement(p.org, p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry], disbursements=[fee])
    time_line = invoice["lines"][0]
    removed = await client.delete(
        org_url(p.org, f"/invoices/{invoice['id']}/lines/{time_line['id']}"),
        headers=p.owner.headers,
    )
    assert removed.status_code == 200
    assert [line["kind"] for line in removed.json()["data"]["lines"]] == ["disbursement"]
    assert removed.json()["data"]["net_total"] == 1_300
    assert (await billing_status("time_entries", [entry.id]))[entry.id] == "unbilled"

    deleted = await client.delete(
        org_url(p.org, f"/invoices/{invoice['id']}"), headers=p.owner.headers
    )
    assert deleted.status_code == 200
    assert (await billing_status("disbursements", [fee.id]))[fee.id] == "unbilled"
    assert await count_rows("SELECT count(*) FROM wip_allocations") == 0
    assert await count_rows("SELECT count(*) FROM invoice_lines") == 0
    gone = await client.get(org_url(p.org, f"/invoices/{invoice['id']}"), headers=p.owner.headers)
    assert gone.status_code == 404
    # The items can be billed again.
    await draft(client, p.owner, p.org, p.client.id, time=[entry], disbursements=[fee])
    assert [
        e["action"] for e in await audit_entries(p.id) if e["action"].startswith("invoice.")
    ] == [
        "invoice.draft_created",
        "invoice.line_removed",
        "invoice.draft_deleted",
        "invoice.draft_created",
    ]


async def test_draft_header_update(client):
    p = await new_practice(client)
    invoice = await draft(client, p.owner, p.org, p.client.id)
    url = org_url(p.org, f"/invoices/{invoice['id']}")
    response = await client.patch(
        url,
        headers=p.owner.headers,
        json={"reference": "PO-77", "notes": "Thanks", "issue_date": "2026-10-01"},
    )
    data = response.json()["data"]
    assert (data["reference"], data["notes"], data["issue_date"]) == (
        "PO-77",
        "Thanks",
        "2026-10-01",
    )
    bad = await client.patch(url, headers=p.owner.headers, json={"due_date": "2026-09-01"})
    assert bad.status_code == 422
    assert (
        await client.patch(url, headers=p.owner.headers, json={"status": "issued"})
    ).status_code == 422


async def test_drafting_permissions(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    manager = await staff(p.org, "manager")
    viewer = await staff(p.org, "viewer")
    staff_member = await staff(p.org, "staff")
    assert (await client.get(p.url("/invoices"), headers=staff_member.headers)).status_code == 403
    await draft(client, viewer, p.org, p.client.id, time=[entry], expect=403)
    invoice = await draft(client, manager, p.org, p.client.id, time=[entry])
    assert (await client.get(p.url("/invoices"), headers=viewer.headers)).status_code == 200
    detail = await client.get(org_url(p.org, f"/invoices/{invoice['id']}"), headers=viewer.headers)
    assert detail.status_code == 200
    line_url = org_url(p.org, f"/invoices/{invoice['id']}/lines")
    denied = await client.post(
        line_url, headers=viewer.headers, json={"description": "x", "unit_amount": 1}
    )
    assert denied.status_code == 403
    deleted = await client.delete(
        org_url(p.org, f"/invoices/{invoice['id']}"), headers=viewer.headers
    )
    assert deleted.status_code == 403
    issued = await client.post(
        org_url(p.org, f"/invoices/{invoice['id']}/issue"), headers=manager.headers
    )
    assert issued.status_code == 403


async def test_other_practice_invoice_is_404(client):
    p = await new_practice(client)
    other = await new_practice(client, "Other Practice")
    entry = await make_time_entry(other.org, other.owner, client=other.client)
    theirs = await draft(client, other.owner, other.org, other.client.id, time=[entry])
    line_id = theirs["lines"][0]["id"]
    base = f"/invoices/{theirs['id']}"
    for method, path, body in (
        ("get", base, None),
        ("patch", base, {"notes": "x"}),
        ("delete", base, None),
        ("post", f"{base}/lines", {"description": "x", "unit_amount": 1}),
        ("patch", f"{base}/lines/{line_id}", {"net_amount": 1}),
        ("delete", f"{base}/lines/{line_id}", None),
        ("post", f"{base}/wip", {"time_entry_ids": [str(entry.id)]}),
        ("post", f"{base}/issue", None),
        ("post", f"{base}/void", {"reason": "x"}),
        ("get", f"{base}/pdf", None),
        ("post", f"{base}/send", None),
    ):
        response = await client.request(
            method, org_url(p.org, path), headers=p.owner.headers, json=body
        )
        assert response.status_code == 404, (method, path, response.text)
    assert (await get_invoice(client, other.owner, other.org, theirs["id"]))["status"] == "draft"
