"""Saving a draft's lines in one request (APT75): kept lines changed, the others removed, new
ones added, and the totals checked once at the end, so a refused save changes nothing."""

from tests.billing_helpers import billing_status, draft, get_invoice, issued, new_practice, org_url
from tests.domain_factories import make_time_entry, staff
from tests.factories import audit_entries, error

FEE = {"description": "Accounts", "unit_amount": 10_000}
DISCOUNT = {"description": "Loyalty discount", "unit_amount": -5_000}


def lines_url(p, invoice: dict) -> str:
    return org_url(p.org, f"/invoices/{invoice['id']}/lines")


async def test_a_smaller_fee_with_a_smallerdiscount_is_saved_at_once(client):
    p = await new_practice(client)
    invoice = await draft(client, p.owner, p.org, p.client.id, manual_lines=[FEE, DISCOUNT])
    fee, discount = invoice["lines"]
    # One line at a time this fails: £40 - £50 is below zero before the discount changes.
    response = await client.put(
        lines_url(p, invoice),
        headers=p.owner.headers,
        json={
            "lines": [
                {"id": fee["id"], "unit_amount": 4_000},
                {"id": discount["id"], "unit_amount": -3_000},
            ]
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [line["net_amount"] for line in data["lines"]] == [4_000, -3_000]
    assert data["net_total"] == 1_000
    updated = await audit_entries(p.id, "invoice.line_updated")
    assert [e["metadata"]["new"]["net_amount"] for e in updated] == [4_000, -3_000]
    assert updated[-1]["metadata"]["net_total"] == 1_000


async def test_lines_are_changed_removed_and_added_together(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    invoice = await draft(
        client, p.owner, p.org, p.client.id, time=[entry], manual_lines=[FEE, DISCOUNT]
    )
    work, fee, discount = invoice["lines"]
    response = await client.put(
        lines_url(p, invoice),
        headers=p.owner.headers,
        json={
            "lines": [{"id": fee["id"], "description": "Annual accounts"}],
            "new_lines": [{"description": "Filing fee", "quantity": "2", "unit_amount": 1_700}],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [(line["description"], line["net_amount"]) for line in data["lines"]] == [
        ("Annual accounts", 10_000),
        ("Filing fee", 3_400),
    ]
    assert data["net_total"] == 13_400
    # The removed line of work is WIP again.
    assert (await billing_status("time_entries", [entry.id]))[entry.id] == "unbilled"
    actions = [e["action"] for e in await audit_entries(p.id) if e["action"].startswith("invoice.")]
    assert actions == [
        "invoice.draft_created",
        "invoice.line_removed",
        "invoice.line_removed",
        "invoice.line_updated",
        "invoice.line_added",
    ]
    removed = await audit_entries(p.id, "invoice.line_removed")
    assert {e["metadata"]["line"]["id"] for e in removed} == {work["id"], discount["id"]}
    # Nothing given for a kept line leaves it as it is, unaudited.
    again = await client.put(
        lines_url(p, invoice),
        headers=p.owner.headers,
        json={"lines": [{"id": line["id"]} for line in data["lines"]]},
    )
    assert again.status_code == 200, again.text
    assert again.json()["data"]["lines"] == data["lines"]
    assert len(await audit_entries(p.id, "invoice.line_updated")) == 1


async def test_a_refused_save_changes_nothing(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry], manual_lines=[FEE])
    work, fee = invoice["lines"]
    # The line of work goes, the fee shrinks and a discount is added: below zero in the end.
    response = await client.put(
        lines_url(p, invoice),
        headers=p.owner.headers,
        json={
            "lines": [{"id": fee["id"], "unit_amount": 1_000}],
            "new_lines": [DISCOUNT],
        },
    )
    assert response.status_code == 422, response.text
    assert error(response)["code"] == "INVOICE_TOTAL_NEGATIVE"
    after = await get_invoice(client, p.owner, p.org, invoice["id"])
    assert after["lines"] == invoice["lines"]
    assert after["net_total"] == invoice["net_total"]
    # The work stays on the draft: it is only unbilled once a save succeeds.
    assert (await billing_status("time_entries", [entry.id]))[entry.id] == "in_draft"
    actions = [e["action"] for e in await audit_entries(p.id) if e["action"].startswith("invoice.")]
    assert actions == ["invoice.draft_created"]
    # A line of work cannot go below zero either, and that refuses the whole save.
    negative = await client.put(
        lines_url(p, invoice),
        headers=p.owner.headers,
        json={"lines": [{"id": work["id"], "net_amount": -1}, {"id": fee["id"]}]},
    )
    assert negative.status_code == 422 and error(negative)["code"] == "NEGATIVE_WIP_LINE"
    assert (await get_invoice(client, p.owner, p.org, invoice["id"]))["lines"] == invoice["lines"]


async def test_saving_lines_needs_a_draft_its_own_lines_and_invoice_create(client):
    p = await new_practice(client)
    invoice = await draft(client, p.owner, p.org, p.client.id, manual_lines=[FEE])
    [fee] = invoice["lines"]
    other = await draft(client, p.owner, p.org, p.client.id, manual_lines=[FEE])
    url = lines_url(p, invoice)
    viewer = await staff(p.org, "viewer")
    denied = await client.put(url, headers=viewer.headers, json={"lines": []})
    assert denied.status_code == 403
    foreign = await client.put(
        url, headers=p.owner.headers, json={"lines": [{"id": other["lines"][0]["id"]}]}
    )
    assert foreign.status_code == 404
    twice = await client.put(
        url, headers=p.owner.headers, json={"lines": [{"id": fee["id"]}, {"id": fee["id"]}]}
    )
    assert twice.status_code == 422
    assert (await client.put(url, headers=p.owner.headers, json={})).status_code == 422
    assert (await get_invoice(client, p.owner, p.org, invoice["id"]))["lines"] == invoice["lines"]
    await issued(client, p.owner, p.org, invoice["id"])
    locked = await client.put(url, headers=p.owner.headers, json={"lines": [{"id": fee["id"]}]})
    assert locked.status_code == 409 and error(locked)["code"] == "INVOICE_NOT_DRAFT"
