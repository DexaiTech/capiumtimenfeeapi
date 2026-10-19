"""Invoice PDFs and the invoice email (queued job, PDF attachment, idempotency)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.db.models import ClientContact
from app.db.session import get_sessionmaker
from app.workers import handlers
from tests.billing_helpers import draft, get_invoice, issued, new_practice, org_url, set_org
from tests.conftest import run_jobs
from tests.domain_factories import make_client, make_time_entry, staff
from tests.factories import audit_entries, error


async def add_contact(client_row, **fields) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            ClientContact(
                organization_id=client_row.organization_id, client_id=client_row.id, **fields
            )
        )
        await session.commit()


async def test_draft_pdf_has_watermark_pound_sign_and_long_text(client):
    p = await new_practice(client)
    await set_org(p.org, bank_details="Sort code 00-00-00, account 12345678", vat_number="GB1")
    long_text = "Preparation of statutory accounts and corporation tax computations " * 12
    entry = await make_time_entry(p.org, p.owner, client=p.client, description=long_text)
    invoice = await draft(
        client,
        p.owner,
        p.org,
        p.client.id,
        time=[entry],
        group_by="entry",
        notes=f"Caf{chr(0xE9)} {chr(0x2013)} {chr(0x201C)}quoted{chr(0x201D)} {chr(0x2713)} notes",
    )
    response = await client.get(
        org_url(p.org, f"/invoices/{invoice['id']}/pdf"), headers=p.owner.headers
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert "inline" in response.headers["content-disposition"]
    body = response.content
    assert body.startswith(b"%PDF")
    assert b"DRAFT" in body
    assert b"\xa3144.00" in body  # the pound sign in Latin-1: £120.00 + VAT = £144.00
    assert b'Caf\xe9 - "quoted" ? notes' in body
    assert b"Sort code 00-00-00" in body


async def test_issued_pdf_carries_the_number_and_no_watermark(client):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    await issued(client, p.owner, p.org, invoice["id"])
    response = await client.get(
        org_url(p.org, f"/invoices/{invoice['id']}/pdf"), headers=p.owner.headers
    )
    assert response.content.startswith(b"%PDF")
    assert b"INV-00001" in response.content
    assert b"DRAFT" not in response.content
    assert 'filename="INV-00001.pdf"' in response.headers["content-disposition"]
    staff_member = await staff(p.org, "staff")
    denied = await client.get(
        org_url(p.org, f"/invoices/{invoice['id']}/pdf"), headers=staff_member.headers
    )
    assert denied.status_code == 403


async def test_non_vat_pdf_renders(client):
    p = await new_practice(client, vat_registered=False)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    response = await client.get(
        org_url(p.org, f"/invoices/{invoice['id']}/pdf"), headers=p.owner.headers
    )
    assert response.content.startswith(b"%PDF") and b"\xa3120.00" in response.content


async def test_send_emails_the_pdf_to_billing_contacts(client, worker, mailbox):
    p = await new_practice(client)
    await add_contact(p.client, name="Pat Primary", email="pat@widgets.example", is_primary=True)
    await add_contact(p.client, name="Bill Payer", email="bills@widgets.example", is_billing=True)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    url = org_url(p.org, f"/invoices/{invoice['id']}/send")
    not_issued = await client.post(url, headers=p.owner.headers)
    assert not_issued.status_code == 409 and error(not_issued)["code"] == "INVOICE_NOT_ISSUED"
    await issued(client, p.owner, p.org, invoice["id"])

    response = await client.post(url, headers=p.owner.headers)
    assert response.status_code == 202, response.text
    assert response.json()["data"] == {
        "invoice_id": invoice["id"],
        "to": ["bills@widgets.example"],
        "queued": True,
    }
    # A second request while the first is queued is not queued again.
    again = await client.post(url, headers=p.owner.headers)
    assert again.json()["data"]["queued"] is False
    assert (await get_invoice(client, p.owner, p.org, invoice["id"]))["sent_at"] is None

    assert await run_jobs(worker) == 1
    [message] = mailbox.to("bills@widgets.example")
    assert message["Subject"] == "Invoice INV-00001 from Acme Accountants"
    assert "Dear Bill Payer" in mailbox.text(message)
    assert "£144.00" in mailbox.text(message)
    [attachment] = [part for part in message.walk() if part.get_content_type() == "application/pdf"]
    assert attachment.get_filename() == "INV-00001.pdf"
    assert attachment.get_payload(decode=True).startswith(b"%PDF")
    assert (await get_invoice(client, p.owner, p.org, invoice["id"]))["sent_at"] is not None
    [log] = await audit_entries(p.id, "invoice.send_queued")
    assert log["metadata"]["to"] == ["bills@widgets.example"]


async def test_send_to_given_addresses_and_fallbacks(client, worker, mailbox):
    p = await new_practice(client)
    nobody = await make_client(p.org, "Nobody Ltd")
    entry = await make_time_entry(p.org, p.owner, client=nobody)
    invoice = await draft(client, p.owner, p.org, nobody.id, time=[entry])
    await issued(client, p.owner, p.org, invoice["id"])
    url = org_url(p.org, f"/invoices/{invoice['id']}/send")
    missing = await client.post(url, headers=p.owner.headers)
    assert missing.status_code == 422 and error(missing)["code"] == "NO_RECIPIENT"
    bad = await client.post(url, headers=p.owner.headers, json={"to": ["not-an-email"]})
    assert bad.status_code == 422
    given = await client.post(url, headers=p.owner.headers, json={"to": ["ceo@nobody.example"]})
    assert given.status_code == 202
    await run_jobs(worker)
    assert len(mailbox.to("ceo@nobody.example")) == 1

    # Primary contact, then client email, when there are no billing contacts.
    emailed = await make_client(p.org, "Emailed Ltd", email="office@emailed.example")
    entry = await make_time_entry(p.org, p.owner, client=emailed)
    invoice = await draft(client, p.owner, p.org, emailed.id, time=[entry])
    await issued(client, p.owner, p.org, invoice["id"])
    response = await client.post(
        org_url(p.org, f"/invoices/{invoice['id']}/send"), headers=p.owner.headers
    )
    assert response.json()["data"]["to"] == ["office@emailed.example"]
    manager = await staff(p.org, "manager")
    denied = await client.post(
        org_url(p.org, f"/invoices/{invoice['id']}/send"), headers=manager.headers
    )
    assert denied.status_code == 403


async def test_the_email_job_is_idempotent(client, worker, mailbox):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    await issued(client, p.owner, p.org, invoice["id"])
    payload = {
        "organization_id": p.id,
        "invoice_id": invoice["id"],
        "to": ["a@example.com"],
        "requested_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    }
    async with get_sessionmaker()() as session:
        result = await handlers.HANDLERS["send_invoice_email"](session, get_settings(), payload)
        await session.commit()
    assert result and "sent_at" in result
    # Delivered again (at-least-once): the request was already served, nothing is sent.
    async with get_sessionmaker()() as session:
        result = await handlers.HANDLERS["send_invoice_email"](session, get_settings(), payload)
        await session.commit()
    assert result and result["skipped"] == "already_sent"
    assert len(mailbox.to("a@example.com")) == 1


async def test_email_job_for_a_voided_invoice_fails_permanently(client, worker, mailbox):
    p = await new_practice(client)
    entry = await make_time_entry(p.org, p.owner, client=p.client)
    invoice = await draft(client, p.owner, p.org, p.client.id, time=[entry])
    await issued(client, p.owner, p.org, invoice["id"])
    await client.post(
        org_url(p.org, f"/invoices/{invoice['id']}/send"),
        headers=p.owner.headers,
        json={"to": ["x@example.com"]},
    )
    await client.post(
        org_url(p.org, f"/invoices/{invoice['id']}/void"),
        headers=p.owner.headers,
        json={"reason": "Mistake"},
    )
    await run_jobs(worker)
    async with get_sessionmaker()() as session:
        status = await session.scalar(
            text("SELECT status FROM background_jobs WHERE type = 'send_invoice_email'")
        )
    assert status == "failed"
    assert mailbox.to("x@example.com") == []
    with pytest.raises(handlers.PermanentJobError):
        async with get_sessionmaker()() as session:
            await handlers.HANDLERS["send_invoice_email"](session, get_settings(), {"to": []})
