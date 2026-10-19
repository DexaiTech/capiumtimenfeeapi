"""The client statement: opening balance, entries with a running balance, closing balance."""

from datetime import date

from tests.billing_helpers import draft, issued, new_practice, org_url, pay
from tests.domain_factories import make_time_entry, staff


async def invoice_on(client, p, issue_date: str, minutes: int) -> dict:
    entry = await make_time_entry(p.org, p.owner, client=p.client, minutes=minutes)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    return await issued(client, p.owner, p.org, invoice["id"], issue_date=issue_date)


async def build_history(client, p) -> dict:
    a = await invoice_on(client, p, "2026-08-10", 60)  # £144.00
    paid_a = await pay(
        client,
        p.owner,
        p.org,
        p.client.id,
        10_000,
        received_on=date(2026, 8, 20),
        reference="BACS 1",
        allocations=[{"invoice_id": a["id"], "amount": 10_000}],
    )
    assert paid_a.status_code == 201
    b = await invoice_on(client, p, "2026-09-05", 30)  # £72.00
    credit = await client.post(
        org_url(p.org, f"/invoices/{b['id']}/credit-notes"),
        headers=p.owner.headers,
        json={
            "reason": "Discount",
            "issue_date": "2026-09-10",
            "lines": [{"description": "Discount", "net_amount": 1_000}],
        },
    )
    assert credit.status_code == 201
    voided = await invoice_on(client, p, "2026-09-12", 600)
    await client.post(
        org_url(p.org, f"/invoices/{voided['id']}/void"),
        headers=p.owner.headers,
        json={"reason": "Raised in error"},
    )
    on_account = await pay(
        client, p.owner, p.org, p.client.id, 5_000, received_on=date(2026, 9, 25)
    )
    bounced = (
        await pay(client, p.owner, p.org, p.client.id, 9_999, received_on=date(2026, 9, 26))
    ).json()["data"]
    await client.post(
        org_url(p.org, f"/payments/{bounced['id']}/void"), headers=p.owner.headers, json={}
    )
    return {"a": a, "b": b, "credit": credit.json()["data"], "on_account": on_account.json()}


async def test_statement_for_a_period(client):
    p = await new_practice(client)
    history = await build_history(client, p)
    response = await client.get(
        org_url(p.org, f"/clients/{p.client.id}/statement"),
        headers=p.owner.headers,
        params={"date_from": "2026-09-01", "date_to": "2026-09-30"},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["opening_balance"] == 14_400 - 10_000
    assert [(e["type"], e["date"], e["amount"], e["balance"]) for e in data["entries"]] == [
        ("invoice", "2026-09-05", 7_200, 11_600),
        ("credit_note", "2026-09-10", -1_200, 10_400),
        ("payment", "2026-09-25", -5_000, 5_400),
    ]
    assert data["entries"][0]["number"] == history["b"]["number"]
    assert data["entries"][1]["number"] == history["credit"]["number"]
    assert (data["invoiced"], data["credited"], data["paid"]) == (7_200, 1_200, 5_000)
    assert data["closing_balance"] == 5_400
    assert data["unallocated_cash"] == 5_000
    assert data["currency"] == "GBP"


async def test_statement_from_the_beginning(client):
    p = await new_practice(client)
    await build_history(client, p)
    response = await client.get(
        org_url(p.org, f"/clients/{p.client.id}/statement"),
        headers=p.owner.headers,
        params={"date_to": "2026-12-31"},
    )
    data = response.json()["data"]
    assert data["opening_balance"] == 0 and data["date_from"] is None
    assert [e["type"] for e in data["entries"]] == [
        "invoice",
        "payment",
        "invoice",
        "credit_note",
        "payment",
    ]
    # The closing balance is what is owed on open invoices less the cash on account.
    owed = (14_400 - 10_000) + (7_200 - 1_200)
    assert data["closing_balance"] == owed - 5_000
    early = await client.get(
        org_url(p.org, f"/clients/{p.client.id}/statement"),
        headers=p.owner.headers,
        params={"date_to": "2026-08-15"},
    )
    assert early.json()["data"]["closing_balance"] == 14_400
    assert early.json()["data"]["unallocated_cash"] == 0
    bad = await client.get(
        org_url(p.org, f"/clients/{p.client.id}/statement"),
        headers=p.owner.headers,
        params={"date_from": "2026-10-01", "date_to": "2026-09-01"},
    )
    assert bad.status_code == 422


async def test_statement_pdf(client):
    p = await new_practice(client)
    await build_history(client, p)
    response = await client.get(
        org_url(p.org, f"/clients/{p.client.id}/statement/pdf"),
        headers=p.owner.headers,
        params={"date_from": "2026-09-01", "date_to": "2026-09-30"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert b"STATEMENT" in response.content
    assert b"\xa354.00" in response.content  # closing balance £54.00
    assert b"Opening balance" in response.content


async def test_statement_permissions_and_tenancy(client):
    p = await new_practice(client)
    staff_member = await staff(p.org, "staff")
    manager = await staff(p.org, "manager")
    url = org_url(p.org, f"/clients/{p.client.id}/statement")
    assert (await client.get(url, headers=staff_member.headers)).status_code == 403
    assert (await client.get(url, headers=manager.headers)).status_code == 200
    other = await new_practice(client, "Other Practice")
    for path in (f"/clients/{p.client.id}/statement", f"/clients/{p.client.id}/statement/pdf"):
        response = await client.get(org_url(other.org, path), headers=other.owner.headers)
        assert response.status_code == 404
