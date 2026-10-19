"""Credit notes against issued invoices."""

import asyncio

from tests.billing_helpers import (
    billing_status,
    draft,
    get_invoice,
    issued,
    new_practice,
    org_url,
    pay,
)
from tests.clients_helpers import count_queries
from tests.domain_factories import make_time_entry, staff, vat_rate_id
from tests.factories import audit_entries, error


async def issued_invoice(client, p, minutes=60, **issue_body):
    entry = await make_time_entry(p.org, p.owner, client=p.client, minutes=minutes)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    data = await issued(client, p.owner, p.org, invoice["id"], **issue_body)
    return data, entry


def credit_url(p, invoice_id):
    return org_url(p.org, f"/invoices/{invoice_id}/credit-notes")


async def test_raise_a_credit_note(client):
    p = await new_practice(client)
    invoice, entry = await issued_invoice(client, p, issue_date="2026-10-01")  # £144 gross
    zero = await vat_rate_id(p.org, "zero")
    response = await client.post(
        credit_url(p, invoice["id"]),
        headers=p.owner.headers,
        json={
            "reason": "Agreed fee reduction",
            "issue_date": "2026-10-05",
            "lines": [
                {"description": "Fee reduction", "net_amount": 2_005},
                {"description": "Postage refund", "net_amount": 300, "vat_rate_id": str(zero)},
            ],
        },
    )
    assert response.status_code == 201, response.text
    note = response.json()["data"]
    assert note["number"] == "CN-00001"
    assert note["invoice_number"] == invoice["number"]
    assert [line["vat_amount"] for line in note["lines"]] == [401, 0]  # 20% of 20.05 = 4.01
    assert (note["net_total"], note["vat_total"], note["gross_total"]) == (2_305, 401, 2_706)
    after = await get_invoice(client, p.owner, p.org, invoice["id"])
    assert after["credited_total"] == 2_706
    assert after["balance"] == 14_400 - 2_706
    assert after["credit_notes"][0]["number"] == "CN-00001"
    # A credit note does not reinstate WIP.
    assert (await billing_status("time_entries", [entry.id]))[entry.id] == "billed"
    [log] = await audit_entries(p.id, "credit_note.issued")
    assert log["metadata"]["gross_total"] == 2_706
    assert log["metadata"]["invoice_number"] == invoice["number"]

    detail = await client.get(
        org_url(p.org, f"/credit-notes/{note['id']}"), headers=p.owner.headers
    )
    assert detail.json()["data"]["lines"][1]["description"] == "Postage refund"
    listed = await client.get(
        org_url(p.org, "/credit-notes"),
        headers=p.owner.headers,
        params={"invoice_id": invoice["id"]},
    )
    assert [n["id"] for n in listed.json()["data"]] == [note["id"]]
    assert listed.json()["data"][0]["lines"] is None
    by_q = await client.get(
        org_url(p.org, "/credit-notes"), headers=p.owner.headers, params={"q": "CN-0000"}
    )
    assert len(by_q.json()["data"]) == 1
    pdf = await client.get(
        org_url(p.org, f"/credit-notes/{note['id']}/pdf"), headers=p.owner.headers
    )
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF") and b"CN-00001" in pdf.content
    assert b"\xa327.06" in pdf.content


async def test_credit_cannot_exceed_the_outstanding_balance(client):
    p = await new_practice(client)
    invoice, _ = await issued_invoice(client, p)  # £144.00
    paid = await pay(
        client,
        p.owner,
        p.org,
        p.client.id,
        10_000,
        allocations=[{"invoice_id": invoice["id"], "amount": 10_000}],
    )
    assert paid.status_code == 201
    # Balance £44.00: a £40 net (£48 gross) credit is too much, £36.67 net (£44.00) fits.
    too_much = await client.post(
        credit_url(p, invoice["id"]),
        headers=p.owner.headers,
        json={"reason": "x", "lines": [{"description": "x", "net_amount": 4_000}]},
    )
    assert too_much.status_code == 409
    assert error(too_much)["code"] == "CREDIT_EXCEEDS_BALANCE"
    assert error(too_much)["details"] == {"gross_total": 4_800, "balance": 4_400}
    exact = await client.post(
        credit_url(p, invoice["id"]),
        headers=p.owner.headers,
        json={"reason": "x", "lines": [{"description": "x", "net_amount": 3_667}]},
    )
    assert exact.status_code == 201, exact.text
    after = await get_invoice(client, p.owner, p.org, invoice["id"])
    assert (after["balance"], after["display_status"]) == (0, "paid")
    nothing_left = await client.post(
        credit_url(p, invoice["id"]),
        headers=p.owner.headers,
        json={"reason": "x", "lines": [{"description": "x", "net_amount": 1}]},
    )
    assert error(nothing_left)["code"] == "CREDIT_EXCEEDS_BALANCE"


async def test_parallel_credit_notes_cannot_over_credit(client):
    p = await new_practice(client)
    invoice, _ = await issued_invoice(client, p)  # £144.00 gross

    async def credit():
        return await client.post(
            credit_url(p, invoice["id"]),
            headers=p.owner.headers,
            json={"reason": "x", "lines": [{"description": "x", "net_amount": 6_000}]},
        )

    # Each is £72.00 gross: two fit the £144.00 balance exactly, the third must fail.
    results = await asyncio.gather(*(credit() for _ in range(3)))
    assert sorted(r.status_code for r in results) == [201, 201, 409]
    numbers = sorted(r.json()["data"]["number"] for r in results if r.status_code == 201)
    assert numbers == ["CN-00001", "CN-00002"]
    after = await get_invoice(client, p.owner, p.org, invoice["id"])
    assert (after["credited_total"], after["balance"]) == (14_400, 0)


async def test_only_issued_invoices_take_credit_notes(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    body = {"reason": "x", "lines": [{"description": "x", "net_amount": 100}]}
    response = await client.post(credit_url(p, invoice["id"]), headers=p.owner.headers, json=body)
    assert response.status_code == 409 and error(response)["code"] == "INVOICE_NOT_ISSUED"
    await issued(client, p.owner, p.org, invoice["id"])
    await client.post(
        org_url(p.org, f"/invoices/{invoice['id']}/void"),
        headers=p.owner.headers,
        json={"reason": "x"},
    )
    response = await client.post(credit_url(p, invoice["id"]), headers=p.owner.headers, json=body)
    assert error(response)["code"] == "INVOICE_NOT_ISSUED"


async def test_validation(client):
    p = await new_practice(client)
    invoice, _ = await issued_invoice(client, p, issue_date="2026-10-01")
    url = credit_url(p, invoice["id"])
    line = {"description": "x", "net_amount": 100}
    for bad in (
        {"reason": "x", "lines": []},
        {"reason": "", "lines": [line]},
        {"reason": "x", "lines": [{"description": "x", "net_amount": 0}]},
        {"reason": "x", "lines": [{"description": "x", "net_amount": -5}]},
        {"reason": "x", "lines": [line], "issue_date": "2026-09-30"},
        {"reason": "x", "lines": [line], "number": "CN-9"},
    ):
        response = await client.post(url, headers=p.owner.headers, json=bad)
        assert response.status_code == 422, bad
    other = await new_practice(client, "Other Practice")
    foreign_rate = await vat_rate_id(other.org)
    response = await client.post(
        url,
        headers=p.owner.headers,
        json={"reason": "x", "lines": [{**line, "vat_rate_id": str(foreign_rate)}]},
    )
    assert response.status_code == 422


async def test_non_vat_practice_credit_has_no_vat(client):
    p = await new_practice(client, vat_registered=False)
    invoice, _ = await issued_invoice(client, p)
    response = await client.post(
        credit_url(p, invoice["id"]),
        headers=p.owner.headers,
        json={"reason": "x", "lines": [{"description": "x", "net_amount": 5_000}]},
    )
    note = response.json()["data"]
    assert (note["vat_total"], note["gross_total"]) == (0, 5_000)


async def test_list_reads_invoice_numbers_without_a_query_per_note(client):
    p = await new_practice(client)
    body = {"reason": "x", "lines": [{"description": "x", "net_amount": 100}]}
    invoices = []
    for _ in range(4):
        invoice, _ = await issued_invoice(client, p)
        invoices.append(invoice)
        created = await client.post(
            credit_url(p, invoice["id"]), headers=p.owner.headers, json=body
        )
        assert created.status_code == 201
    url = org_url(p.org, "/credit-notes")
    with count_queries() as one:
        single = await client.get(url, headers=p.owner.headers, params={"limit": 1})
    with count_queries() as many:
        listed = await client.get(url, headers=p.owner.headers)
    assert len(many) == len(one)
    numbers = {n["invoice_id"]: n["invoice_number"] for n in listed.json()["data"]}
    assert numbers == {i["id"]: i["number"] for i in invoices}
    assert len(single.json()["data"]) == 1


async def test_permissions_and_tenancy(client):
    p = await new_practice(client)
    invoice, _ = await issued_invoice(client, p)
    body = {"reason": "x", "lines": [{"description": "x", "net_amount": 100}]}
    manager = await staff(p.org, "manager")
    viewer = await staff(p.org, "viewer")
    billing = await staff(p.org, "billing")
    staff_member = await staff(p.org, "staff")
    for denied in (manager, viewer):
        response = await client.post(
            credit_url(p, invoice["id"]), headers=denied.headers, json=body
        )
        assert response.status_code == 403
    created = await client.post(credit_url(p, invoice["id"]), headers=billing.headers, json=body)
    assert created.status_code == 201
    note_id = created.json()["data"]["id"]
    assert (
        await client.get(org_url(p.org, f"/credit-notes/{note_id}"), headers=viewer.headers)
    ).status_code == 200
    assert (
        await client.get(org_url(p.org, "/credit-notes"), headers=staff_member.headers)
    ).status_code == 403

    other = await new_practice(client, "Other Practice")
    for path in (f"/credit-notes/{note_id}", f"/credit-notes/{note_id}/pdf"):
        response = await client.get(org_url(other.org, path), headers=other.owner.headers)
        assert response.status_code == 404
    response = await client.post(
        credit_url(other, invoice["id"]), headers=other.owner.headers, json=body
    )
    assert response.status_code == 404
    listed = await client.get(org_url(other.org, "/credit-notes"), headers=other.owner.headers)
    assert listed.json()["data"] == []
