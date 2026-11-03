"""APT71: voiding an invoice can email the client, and a void invoice can be deleted.

The delete of a void invoice is soft: it leaves the invoice list, its counts and the client's
figures, but the row, its number and its audit trail are kept, and it still opens by id.
"""

from tests.billing_helpers import count_rows, draft, get_invoice, issued, new_practice, org_url
from tests.conftest import run_jobs
from tests.domain_factories import make_client, make_time_entry, staff
from tests.factories import audit_entries, error
from tests.test_member_permissions import put


async def _issued(client, p, client_row=None) -> dict:
    row = client_row or p.client
    entry = await make_time_entry(p.org, p.owner, client=row)
    invoice = await draft(client, p.owner, p.org, row.id, time=[entry])
    return await issued(client, p.owner, p.org, invoice["id"], issue_date="2026-10-05")


async def _void(client, actor, org, invoice_id: str, **body):
    return await client.post(
        org_url(org, f"/invoices/{invoice_id}/void"),
        headers=actor.headers,
        json={"reason": "Raised in error", **body},
    )


async def _delete(client, actor, org, invoice_id: str):
    return await client.delete(org_url(org, f"/invoices/{invoice_id}"), headers=actor.headers)


async def _listed(client, p, **params) -> list[str]:
    response = await client.get(org_url(p.org, "/invoices"), headers=p.owner.headers, params=params)
    assert response.status_code == 200, response.text
    return [i["id"] for i in response.json()["data"]]


# --- The email to the client ------------------------------------------------------------------


async def test_voiding_emails_the_client_when_asked(client, worker, mailbox):
    p = await new_practice(client)
    emailed = await make_client(p.org, "Emailed Ltd", email="office@emailed.example")
    invoice = await _issued(client, p, emailed)
    detail = await get_invoice(client, p.owner, p.org, invoice["id"])
    assert detail["email_recipients"] == ["office@emailed.example"]

    response = await _void(client, p.owner, p.org, invoice["id"], notify_client=True)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "void"
    assert await run_jobs(worker) == 1
    [message] = mailbox.to("office@emailed.example")
    assert message["Subject"] == "Invoice INV-00001 from Acme Accountants has been cancelled"
    text = mailbox.text(message)
    assert "INV-00001" in text and "cancelled" in text and "£144.00" in text
    [log] = await audit_entries(p.id, "invoice.void_email_queued")
    assert log["metadata"]["to"] == ["office@emailed.example"]
    assert log["metadata"]["number"] == "INV-00001"


async def test_voiding_without_the_box_ticked_sends_nothing(client, worker, mailbox):
    p = await new_practice(client)
    emailed = await make_client(p.org, "Emailed Ltd", email="office@emailed.example")
    invoice = await _issued(client, p, emailed)
    response = await _void(client, p.owner, p.org, invoice["id"])
    assert response.status_code == 200, response.text
    await run_jobs(worker)
    assert mailbox.to("office@emailed.example") == []
    assert await audit_entries(p.id, "invoice.void_email_queued") == []
    assert (
        await count_rows(
            "SELECT count(*) FROM background_jobs WHERE type = 'send_invoice_void_email'"
        )
        == 0
    )


async def test_the_email_needs_an_address_and_invoice_send(client):
    p = await new_practice(client)  # Widgets Ltd has no email and no contacts
    invoice = await _issued(client, p)
    assert (await get_invoice(client, p.owner, p.org, invoice["id"]))["email_recipients"] == []
    nobody = await _void(client, p.owner, p.org, invoice["id"], notify_client=True)
    assert nobody.status_code == 422 and error(nobody)["code"] == "NO_RECIPIENT"
    # Nothing was voided.
    assert (await get_invoice(client, p.owner, p.org, invoice["id"]))["status"] == "issued"

    emailed = await make_client(p.org, "Emailed Ltd", email="office@emailed.example")
    other = await _issued(client, p, emailed)
    biller = await staff(p.org, "billing")  # issues, voids and sends
    await put(client, p.owner, p.org, biller, denies=["invoice.send"])
    denied = await _void(client, biller, p.org, other["id"], notify_client=True)
    assert denied.status_code == 403
    assert (await _void(client, biller, p.org, other["id"])).status_code == 200


async def test_the_practice_can_word_the_email_its_own_way(client, worker, mailbox):
    p = await new_practice(client)
    emailed = await make_client(p.org, "Emailed Ltd", email="office@emailed.example")
    invoice = await _issued(client, p, emailed)
    saved = await client.put(
        org_url(p.org, "/email-templates/invoice_void"),
        headers=p.owner.headers,
        json={
            "subject": "Cancelled: {{ invoice_number }}",
            "html_body": "<p>{{ invoice_number }} for {{ client_name }} is cancelled.</p>",
            "text_body": "{{ invoice_number }} for {{ client_name }} is cancelled.",
        },
    )
    assert saved.status_code == 200, saved.text
    await _void(client, p.owner, p.org, invoice["id"], notify_client=True)
    await run_jobs(worker)
    [message] = mailbox.to("office@emailed.example")
    assert message["Subject"] == "Cancelled: INV-00001"
    assert "INV-00001 for Emailed Ltd is cancelled." in mailbox.text(message)


# --- Deleting a void invoice -------------------------------------------------------------------


async def test_deleting_a_void_invoice_hides_it_and_keeps_its_number(client):
    p = await new_practice(client)
    invoice = await _issued(client, p)
    assert (await _void(client, p.owner, p.org, invoice["id"])).status_code == 200
    assert invoice["id"] in await _listed(client, p, status="void")

    response = await _delete(client, p.owner, p.org, invoice["id"])
    assert response.status_code == 200, response.text
    assert response.json()["message"] == "Invoice deleted"
    # Gone from the list, its Void tab, its counts and the client's invoices.
    assert await _listed(client, p) == []
    assert await _listed(client, p, status="void") == []
    assert await _listed(client, p, client_id=str(p.client.id)) == []
    summary = await client.get(org_url(p.org, "/invoices/summary"), headers=p.owner.headers)
    assert summary.json()["data"]["counts"]["void"] == 0
    assert summary.json()["data"]["counts"]["total"] == 0
    overview = await client.get(
        org_url(p.org, f"/clients/{p.client.id}/overview"), headers=p.owner.headers
    )
    assert overview.json()["data"]["invoices"]["count"] == 0
    # Kept for the audit trail: it opens by id, number and lines intact.
    kept = await get_invoice(client, p.owner, p.org, invoice["id"])
    assert (kept["status"], kept["number"]) == ("void", "INV-00001")
    assert kept["deleted_at"] is not None and len(kept["lines"]) == 1
    assert (await count_rows("SELECT count(*) FROM invoices WHERE id = :i", i=invoice["id"])) == 1
    [log] = await audit_entries(p.id, "invoice.deleted")
    assert log["metadata"]["number"] == "INV-00001"
    # Deleted once only; the numbering goes on without a gap.
    again = await _delete(client, p.owner, p.org, invoice["id"])
    assert again.status_code == 404
    assert (await _issued(client, p))["number"] == "INV-00002"


async def test_an_issued_invoice_must_be_voided_before_it_is_deleted(client):
    p = await new_practice(client)
    invoice = await _issued(client, p)
    refused = await _delete(client, p.owner, p.org, invoice["id"])
    assert refused.status_code == 409 and error(refused)["code"] == "INVOICE_NOT_DRAFT"
    assert "Void it first" in error(refused)["message"]
    assert (await get_invoice(client, p.owner, p.org, invoice["id"]))["deleted_at"] is None
    assert invoice["id"] in await _listed(client, p)


async def test_bulk_deletes_drafts_and_void_invoices_but_not_issued_ones(client):
    p = await new_practice(client)
    a_draft = await draft(client, p.owner, p.org, p.client.id)
    void = await _issued(client, p)
    await _void(client, p.owner, p.org, void["id"])
    live = await _issued(client, p)
    url = org_url(p.org, "/invoices/bulk")

    body = {"invoice_ids": [a_draft["id"], void["id"], live["id"]], "action": "delete"}
    refused = await client.post(url, headers=p.owner.headers, json=body)
    assert refused.status_code == 409, refused.text
    details = error(refused)["details"]
    assert [d["invoice_id"] for d in details] == [live["id"]]
    assert details[0]["reason"] == "Void it first: only drafts and void invoices can be deleted."
    assert len(await _listed(client, p)) == 3

    body = {"invoice_ids": [a_draft["id"], void["id"]], "action": "delete"}
    done = await client.post(url, headers=p.owner.headers, json=body)
    assert done.status_code == 200, done.text
    assert done.json()["data"] == {"action": "delete", "affected": 2, "changed": 2}
    assert await _listed(client, p) == [live["id"]]
    [log] = await audit_entries(p.id, "invoice.deleted")
    assert log["metadata"]["bulk"] is True
    assert len(await audit_entries(p.id, "invoice.draft_deleted")) == 1
    # The deleted void invoice is not found by a second bulk delete.
    gone = await client.post(
        url, headers=p.owner.headers, json={"invoice_ids": [void["id"]], "action": "delete"}
    )
    assert gone.status_code == 404


async def test_deleting_a_void_invoice_needs_invoice_issue(client):
    p = await new_practice(client)
    invoice = await _issued(client, p)
    await _void(client, p.owner, p.org, invoice["id"])
    manager = await staff(p.org, "manager")  # prepares drafts, may not issue or void
    assert (await _delete(client, manager, p.org, invoice["id"])).status_code == 403
    bulk = await client.post(
        org_url(p.org, "/invoices/bulk"),
        headers=manager.headers,
        json={"invoice_ids": [invoice["id"]], "action": "delete"},
    )
    assert bulk.status_code == 403
    assert invoice["id"] in await _listed(client, p)
