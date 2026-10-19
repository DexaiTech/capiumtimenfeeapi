"""End to end: WIP at different rates -> draft with a write-off -> issue (concurrently,
gapless) -> part payment -> credit note -> final payment -> balance 0, with every figure
reconciling to the penny. Then the void path reinstates WIP."""

import asyncio
from datetime import date

from sqlalchemy import text

from app.db.session import get_sessionmaker
from tests.billing_helpers import (
    billing_status,
    draft,
    get_invoice,
    hourly_job,
    issue,
    new_practice,
    org_url,
    pay,
)
from tests.domain_factories import make_disbursement, make_time_entry, staff
from tests.factories import audit_entries


async def allocation_rows(invoice_id: str) -> list[tuple[int, int]]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            text(
                "SELECT wip_value, billed_value FROM wip_allocations "
                "WHERE invoice_id = :id AND reversed_at IS NULL ORDER BY wip_value DESC"
            ),
            {"id": invoice_id},
        )
        return [(r[0], r[1]) for r in rows]


async def test_full_reconciliation(client):
    p = await new_practice(client)
    partner = await staff(p.org, "partner", full_name="Pat Partner")
    junior = await staff(p.org, "staff", full_name="Jo Junior")
    accounts = await hourly_job(p, name="Accounts 2026")
    advice = await hourly_job(p, name="Tax advice")

    # WIP: 2h of partner time at £150, 1.5h of junior time at £90, 45 minutes at £60.
    t1 = await make_time_entry(
        p.org, partner, client=p.client, job=accounts, minutes=120, charge_rate=15_000
    )
    t2 = await make_time_entry(
        p.org, junior, client=p.client, job=accounts, minutes=90, charge_rate=9_000
    )
    t3 = await make_time_entry(
        p.org, junior, client=p.client, job=accounts, minutes=45, charge_rate=6_000
    )
    fee = await make_disbursement(p.org, p.client, 1_300, job=accounts)
    t4 = await make_time_entry(p.org, partner, client=p.client, job=advice, minutes=60)

    [wip] = (await client.get(p.url("/wip"), headers=p.owner.headers)).json()["data"]
    assert wip["time_value"] == 30_000 + 13_500 + 4_500 + 12_000
    assert wip["disbursement_value"] == 1_300

    # Bill the accounts job at £450.00 instead of its £480.00 WIP: a £30.00 write-off.
    main = await draft(
        client,
        p.owner,
        p.org,
        p.client.id,
        time=[t1, t2, t3],
        disbursements=[fee],
        overrides=[{"job_id": str(accounts.id), "net_amount": 45_000}],
        reference="Year end 2026",
    )
    time_line, fee_line = main["lines"]
    assert (time_line["net_amount"], time_line["vat_amount"]) == (45_000, 9_000)
    assert time_line["wip"]["adjustment"] == -3_000
    assert (fee_line["net_amount"], fee_line["vat_amount"]) == (1_300, 260)
    assert (main["net_total"], main["vat_total"], main["gross_total"]) == (46_300, 9_260, 55_560)
    # 45,000 over 30,000 / 13,500 / 4,500: 28,125 + 12,656.25 + 4,218.75 -> largest remainder.
    assert await allocation_rows(main["id"]) == [
        (30_000, 28_125),
        (13_500, 12_656),
        (4_500, 4_219),
        (1_300, 1_300),
    ]
    side = await draft(client, p.owner, p.org, p.client.id, time=[t4])

    # Issue both at once: the numbers are taken in turn, with no gap and no duplicate.
    results = await asyncio.gather(
        issue(client, p.owner, p.org, main["id"], issue_date="2026-10-01"),
        issue(client, p.owner, p.org, side["id"], issue_date="2026-10-02"),
    )
    assert [r.status_code for r in results] == [200, 200]
    assert sorted(r.json()["data"]["number"] for r in results) == ["INV-00001", "INV-00002"]
    main = results[0].json()["data"]
    assert main["due_date"] == "2026-10-31"
    assert set((await billing_status("time_entries", [t1.id, t2.id, t3.id])).values()) == {"billed"}
    assert (await client.get(p.url("/wip"), headers=p.owner.headers)).json()["data"] == []

    # Part payment.
    first = await pay(
        client,
        p.owner,
        p.org,
        p.client.id,
        20_000,
        received_on=date(2026, 10, 10),
        allocations=[{"invoice_id": main["id"], "amount": 20_000}],
    )
    assert first.status_code == 201
    after_first = await get_invoice(client, p.owner, p.org, main["id"])
    assert (after_first["balance"], after_first["display_status"]) == (35_560, "part_paid")

    # Credit note: £20.00 + VAT.
    credit = await client.post(
        org_url(p.org, f"/invoices/{main['id']}/credit-notes"),
        headers=p.owner.headers,
        json={
            "reason": "Goodwill",
            "issue_date": "2026-10-12",
            "lines": [{"description": "Goodwill reduction", "net_amount": 2_000}],
        },
    )
    assert credit.status_code == 201, credit.text
    assert credit.json()["data"]["gross_total"] == 2_400
    assert (await get_invoice(client, p.owner, p.org, main["id"]))["balance"] == 33_160

    # The final payment clears both invoices: auto-allocation pays the oldest first.
    side_gross = results[1].json()["data"]["gross_total"]
    final = await pay(
        client,
        p.owner,
        p.org,
        p.client.id,
        33_160 + side_gross,
        received_on=date(2026, 10, 20),
        auto_allocate=True,
    )
    assert final.status_code == 201, final.text
    assert [(a["invoice_id"], a["amount"]) for a in final.json()["data"]["allocations"]] == [
        (main["id"], 33_160),
        (side["id"], side_gross),
    ]
    assert final.json()["data"]["unallocated"] == 0
    settled = await get_invoice(client, p.owner, p.org, main["id"])
    side_now = await get_invoice(client, p.owner, p.org, side["id"])
    for invoice in (settled, side_now):
        assert (invoice["balance"], invoice["display_status"]) == (0, "paid")
        assert invoice["paid_total"] + invoice["credited_total"] == invoice["gross_total"]
    assert settled["paid_total"] == 20_000 + 33_160 and settled["credited_total"] == 2_400
    assert settled["wip"] == {"wip_value": 49_300, "billed_value": 46_300, "adjustment": -3_000}

    # Everything reconciles: invoiced = received + credited, nothing owed, nothing on account.
    received = 20_000 + 33_160 + side_gross
    statement = (
        await client.get(
            org_url(p.org, f"/clients/{p.client.id}/statement"),
            headers=p.owner.headers,
            params={"date_to": "2026-10-31"},
        )
    ).json()["data"]
    assert statement["invoiced"] == 55_560 + side_gross
    assert statement["paid"] == received and statement["credited"] == 2_400
    assert statement["invoiced"] == statement["paid"] + statement["credited"]
    assert (statement["closing_balance"], statement["unallocated_cash"]) == (0, 0)

    actions = [e["action"] for e in await audit_entries(p.id)]
    for expected in (
        "invoice.draft_created",
        "invoice.issued",
        "payment.recorded",
        "payment.allocated",
        "credit_note.issued",
    ):
        assert expected in actions


async def test_void_reinstates_wip_for_rebilling(client):
    p = await new_practice(client)
    job = await hourly_job(p)
    t1 = await make_time_entry(p.org, p.owner, client=p.client, job=job, minutes=90)
    fee = await make_disbursement(p.org, p.client, 2_500)
    invoice = await draft(
        client,
        p.owner,
        p.org,
        p.client.id,
        time=[t1],
        disbursements=[fee],
        overrides=[{"job_id": str(job.id), "net_amount": 15_000}],
    )
    issued = (await issue(client, p.owner, p.org, invoice["id"])).json()["data"]
    voided = await client.post(
        org_url(p.org, f"/invoices/{issued['id']}/void"),
        headers=p.owner.headers,
        json={"reason": "Billed the wrong entity"},
    )
    assert voided.status_code == 200
    # Back in WIP at the original WIP value, not the billed one.
    [row] = (await client.get(p.url("/wip"), headers=p.owner.headers)).json()["data"]
    assert (row["time_value"], row["disbursement_value"]) == (18_000, 2_500)
    rebill = await draft(client, p.owner, p.org, p.client.id, time=[t1], disbursements=[fee])
    assert rebill["net_total"] == 20_500
    # The void invoice keeps its number and its (reversed) allocations for the audit trail.
    void_invoice = await get_invoice(client, p.owner, p.org, issued["id"])
    assert void_invoice["number"] == "INV-00001"
    assert void_invoice["wip"]["billed_value"] == 15_000 + 2_500
