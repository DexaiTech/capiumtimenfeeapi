"""Issuing (numbering, dates, immutability), voiding, listing and display status."""

import asyncio
from datetime import date, timedelta

from tests.billing_helpers import (
    billing_status,
    count_rows,
    draft,
    get_invoice,
    issue,
    issued,
    new_practice,
    org_url,
    pay,
)
from tests.domain_factories import make_client, make_disbursement, make_time_entry, staff
from tests.factories import audit_entries, error


async def test_issue_numbers_dates_and_bills_wip(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    fee = await make_disbursement(p.org, p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry], disbursements=[fee])
    data = await issued(client, p.owner, p.org, invoice["id"], issue_date="2026-10-05")
    assert data["number"] == "INV-00001"
    assert data["status"] == "issued"
    assert (data["issue_date"], data["due_date"]) == ("2026-10-05", "2026-11-04")  # 30 days
    assert data["balance"] == data["gross_total"]
    assert (await billing_status("time_entries", [entry.id]))[entry.id] == "billed"
    assert (await billing_status("disbursements", [fee.id]))[fee.id] == "billed"
    [entry_log] = await audit_entries(p.id, "invoice.issued")
    assert entry_log["metadata"]["number"] == "INV-00001"
    assert entry_log["metadata"]["gross_total"] == data["gross_total"]

    # Client payment terms beat the practice's; the draft's issue date is used.
    quick = await make_client(p.org, "Quick Pay Ltd", payment_terms_days=7)
    other = await make_time_entry(p.org, p.owner, client=quick)
    second = await draft(
        client, p.owner, p.org, quick.id, time=[other], issue_date="2026-10-10", reference="PO-1"
    )
    data = await issued(client, p.owner, p.org, second["id"])
    assert (data["number"], data["issue_date"], data["due_date"]) == (
        "INV-00002",
        "2026-10-10",
        "2026-10-17",
    )


async def test_issue_rules(client):
    p = await new_practice(client)
    empty = await draft(client, p.owner, p.org, p.client.id)
    response = await issue(client, p.owner, p.org, empty["id"])
    assert response.status_code == 409 and error(response)["code"] == "INVOICE_EMPTY"
    bad_due = await client.post(
        org_url(p.org, f"/invoices/{empty['id']}/lines"),
        headers=p.owner.headers,
        json={"description": "Advice", "unit_amount": 10_000},
    )
    assert bad_due.status_code == 201
    response = await issue(
        client, p.owner, p.org, empty["id"], issue_date="2026-10-10", due_date="2026-10-01"
    )
    assert response.status_code == 422
    data = await issued(client, p.owner, p.org, empty["id"], due_date="2026-12-31")
    assert data["due_date"] == "2026-12-31"
    again = await issue(client, p.owner, p.org, empty["id"])
    assert again.status_code == 409 and error(again)["code"] == "INVOICE_NOT_DRAFT"
    # A failed issue does not burn a number.
    assert (
        await count_rows(
            "SELECT next_number FROM document_sequences WHERE organization_id = :o AND kind = 'invoice'",
            o=p.id,
        )
        == 2
    )


async def test_issued_invoices_are_immutable(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    data = await issued(client, p.owner, p.org, invoice["id"])
    base = f"/invoices/{invoice['id']}"
    line_id = data["lines"][0]["id"]
    for method, path, body in (
        ("post", f"{base}/lines", {"description": "x", "unit_amount": 1}),
        ("patch", f"{base}/lines/{line_id}", {"net_amount": 1}),
        ("delete", f"{base}/lines/{line_id}", None),
        ("post", f"{base}/wip", {"time_entry_ids": [str(entry.id)]}),
        ("delete", base, None),
    ):
        response = await client.request(
            method, org_url(p.org, path), headers=p.owner.headers, json=body
        )
        assert response.status_code == 409, (method, path)
        assert error(response)["code"] == "INVOICE_NOT_DRAFT"
    # Nothing on the header can change either, not even the notes or the reference.
    for body in (
        {"issue_date": "2026-01-01"},
        {"notes": "Paid by BACS please"},
        {"reference": "X"},
    ):
        locked = await client.patch(org_url(p.org, base), headers=p.owner.headers, json=body)
        assert locked.status_code == 409 and error(locked)["code"] == "INVOICE_NOT_EDITABLE", body
    assert (await get_invoice(client, p.owner, p.org, invoice["id"]))["notes"] is None
    # A caller without invoice.create is refused before the status is looked at.
    viewer = await staff(p.org, "viewer")
    denied = await client.patch(
        org_url(p.org, base), headers=viewer.headers, json={"reference": "X"}
    )
    assert denied.status_code == 403


async def test_concurrent_issues_take_gapless_numbers(client):
    p = await new_practice(client)
    drafts = []
    for _ in range(6):
        entry = await make_time_entry(p.org, p.owner, client=p.client)
        drafts.append(await draft(client, p.owner, p.org, p.client.id, time=[entry]))
    # One of them twice: exactly one of those two requests wins.
    targets = [d["id"] for d in drafts] + [drafts[0]["id"]]
    results = await asyncio.gather(*(issue(client, p.owner, p.org, i) for i in targets))
    codes = sorted(r.status_code for r in results)
    assert codes == [200] * 6 + [409]
    numbers = sorted(r.json()["data"]["number"] for r in results if r.status_code == 200)
    assert numbers == [f"INV-{n:05d}" for n in range(1, 7)]


async def test_void_reinstates_wip_and_keeps_the_number(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    fee = await make_disbursement(p.org, p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry], disbursements=[fee])
    await issued(client, p.owner, p.org, invoice["id"])
    url = org_url(p.org, f"/invoices/{invoice['id']}/void")
    assert (await client.post(url, headers=p.owner.headers, json={})).status_code == 422
    response = await client.post(url, headers=p.owner.headers, json={"reason": "Wrong client"})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["status"], data["number"], data["display_status"]) == (
        "void",
        "INV-00001",
        "void",
    )
    assert data["void_reason"] == "Wrong client" and data["balance"] == 0
    assert (await billing_status("time_entries", [entry.id]))[entry.id] == "unbilled"
    assert (await billing_status("disbursements", [fee.id]))[fee.id] == "unbilled"
    assert (
        await count_rows(
            "SELECT count(*) FROM wip_allocations WHERE invoice_id = :i AND reversed_at IS NOT NULL",
            i=invoice["id"],
        )
        == 2
    )
    # The void invoice still shows what it had billed.
    assert data["wip"]["billed_value"] == data["net_total"]
    again = await client.post(url, headers=p.owner.headers, json={"reason": "x"})
    assert again.status_code == 409 and error(again)["code"] == "INVOICE_NOT_ISSUED"
    # The WIP is back and bills on a new invoice with the next number.
    [row] = (await client.get(p.url("/wip"), headers=p.owner.headers)).json()["data"]
    assert row["total"] == 12_000 + 1_300
    rebill = await draft(client, p.owner, p.org, p.client.id, time=[entry], disbursements=[fee])
    assert (await issued(client, p.owner, p.org, rebill["id"]))["number"] == "INV-00002"
    [log] = await audit_entries(p.id, "invoice.voided")
    assert log["metadata"]["wip_reinstated"] == 13_300


async def test_void_rules(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    url = org_url(p.org, f"/invoices/{invoice['id']}/void")
    not_issued = await client.post(url, headers=p.owner.headers, json={"reason": "x"})
    assert error(not_issued)["code"] == "INVOICE_NOT_ISSUED"
    await issued(client, p.owner, p.org, invoice["id"])
    paid = await pay(
        client,
        p.owner,
        p.org,
        p.client.id,
        1_000,
        allocations=[{"invoice_id": invoice["id"], "amount": 1_000}],
    )
    assert paid.status_code == 201, paid.text
    response = await client.post(url, headers=p.owner.headers, json={"reason": "x"})
    assert response.status_code == 409 and error(response)["code"] == "INVOICE_HAS_PAYMENTS"
    payment = paid.json()["data"]
    allocation_id = payment["allocations"][0]["id"]
    await client.delete(
        org_url(p.org, f"/payments/{payment['id']}/allocations/{allocation_id}"),
        headers=p.owner.headers,
    )
    credit = await client.post(
        org_url(p.org, f"/invoices/{invoice['id']}/credit-notes"),
        headers=p.owner.headers,
        json={"reason": "Goodwill", "lines": [{"description": "Goodwill", "net_amount": 100}]},
    )
    assert credit.status_code == 201, credit.text
    response = await client.post(url, headers=p.owner.headers, json={"reason": "x"})
    assert response.status_code == 409 and error(response)["code"] == "INVOICE_HAS_CREDITS"
    manager = await staff(p.org, "manager")
    assert (
        await client.post(url, headers=manager.headers, json={"reason": "x"})
    ).status_code == 403


async def _issued_invoice(client, p, client_row, *, issue_date, minutes=60):
    entry = await make_time_entry(p.org, p.owner, client=client_row, minutes=minutes)
    invoice = await draft(client, p.owner, p.org, client_row.id, time=[entry])
    return await issued(client, p.owner, p.org, invoice["id"], issue_date=issue_date)


async def test_list_filters_and_display_statuses(client):
    p = await new_practice(client)
    today = date.today()
    future = (today + timedelta(days=1)).isoformat()
    old = (today - timedelta(days=60)).isoformat()
    other = await make_client(p.org, "Zebra Holdings")
    draft_only = await draft(client, p.owner, p.org, p.client.id)
    current = await _issued_invoice(client, p, p.client, issue_date=future)
    overdue = await _issued_invoice(client, p, p.client, issue_date=old)
    part = await _issued_invoice(client, p, other, issue_date=future)
    paid = await _issued_invoice(client, p, other, issue_date=old)
    void = await _issued_invoice(client, p, other, issue_date=future)
    await client.post(
        org_url(p.org, f"/invoices/{void['id']}/void"),
        headers=p.owner.headers,
        json={"reason": "x"},
    )
    for invoice, amount in ((part, 1_000), (paid, paid["gross_total"])):
        response = await pay(
            client,
            p.owner,
            p.org,
            other.id,
            amount,
            allocations=[{"invoice_id": invoice["id"], "amount": amount}],
        )
        assert response.status_code == 201, response.text

    async def listed(**params):
        response = await client.get(p.url("/invoices"), headers=p.owner.headers, params=params)
        assert response.status_code == 200, response.text
        return {i["id"]: i["display_status"] for i in response.json()["data"]}

    everything = await listed()
    assert everything == {
        draft_only["id"]: "draft",
        current["id"]: "issued",
        overdue["id"]: "overdue",
        part["id"]: "part_paid",
        paid["id"]: "paid",
        void["id"]: "void",
    }
    assert list(await listed(status="overdue")) == [overdue["id"]]
    assert set(await listed(status=["part_paid", "paid"])) == {part["id"], paid["id"]}
    assert set(await listed(outstanding="true")) == {current["id"], overdue["id"], part["id"]}
    assert set(await listed(client_id=str(other.id))) == {part["id"], paid["id"], void["id"]}
    assert set(await listed(q="zebra")) == {part["id"], paid["id"], void["id"]}
    assert list(await listed(q=overdue["number"])) == [overdue["id"]]
    assert set(await listed(date_to=(today - timedelta(days=1)).isoformat())) == {
        overdue["id"],
        paid["id"],
    }
    bad = await client.get(p.url("/invoices"), headers=p.owner.headers, params={"status": "nope"})
    assert bad.status_code == 422

    page1 = await client.get(p.url("/invoices"), headers=p.owner.headers, params={"limit": 4})
    body = page1.json()
    assert len(body["data"]) == 4 and body["meta"]["has_more"]
    page2 = await client.get(
        p.url("/invoices"),
        headers=p.owner.headers,
        params={"limit": 4, "cursor": body["meta"]["next_cursor"]},
    )
    ids = [i["id"] for i in body["data"]] + [i["id"] for i in page2.json()["data"]]
    assert sorted(ids) == sorted(everything)

    detail = await get_invoice(client, p.owner, p.org, part["id"])
    assert detail["paid_total"] == 1_000
    assert detail["balance"] == detail["gross_total"] - 1_000
    assert detail["payments"][0]["amount"] == 1_000


async def test_issue_with_zero_payment_terms_is_due_on_issue(client):
    p = await new_practice(client, payment_terms_days=0)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    data = await issued(client, p.owner, p.org, invoice["id"], issue_date="2026-10-01")
    assert data["due_date"] == "2026-10-01"
