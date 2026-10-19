"""Bookkeeping: find the issued invoices and credit notes that have not been queued, and send one.

Nothing here is called from billing. A pull-style scan (the periodic worker job and "run sync now")
queues every issued document that has no sync item, so the billing code needs no hook and a failure
of bookkeeping can never reach an invoice: a document that cannot be sent stays a pending or failed
item with the reason, and billing carries on.

A send checks every line against the mappings first (the income account of its service, the tax code
of its VAT code) and, if anything is unmapped, fails the item at once and says which (``UNMAPPED``:
retrying cannot help until the mapping is added). Credit note lines carry no service, so they are
booked to the account mapped for lines without a service.
"""

import uuid
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IntegrationSyncItem
from app.db.session import unit_of_work
from app.repositories.bookkeeping_sync import BookkeepingRepository, DocumentData, Mappings
from app.services.integration_outbox import ItemFailure, ItemOutcome
from app.services.integrations import (
    BookkeepingDocument,
    BookkeepingLine,
    get_bookkeeping_gateway,
)

SCAN_LIMIT = 500


async def scan_documents(
    session: AsyncSession, organization_id: uuid.UUID, config: dict[str, Any]
) -> int:
    """Queue the issued documents that have no sync item yet; how many were queued."""
    send_from = date.fromisoformat(config["send_from"]) if config.get("send_from") else None
    repo = BookkeepingRepository(session)
    queued = 0
    async with unit_of_work(session):
        if config.get("send_invoices", True):
            queued += await repo.queue_invoices(organization_id, send_from, SCAN_LIMIT)
        if config.get("send_credit_notes", True):
            queued += await repo.queue_credit_notes(organization_id, send_from, SCAN_LIMIT)
    return queued


def _unmapped_message(accounts: list[str], taxes: list[str]) -> str:
    parts = []
    if accounts:
        parts.append("income account for " + ", ".join(accounts))
    if taxes:
        parts.append("tax code for " + ", ".join(taxes))
    return (
        "Not mapped: "
        + "; ".join(parts)
        + ". Add the mapping in the Bookkeeping settings, then retry."
    )


def build_document(data: DocumentData, mappings: Mappings) -> BookkeepingDocument:
    """The document as bookkeeping takes it; ``ItemFailure`` ``UNMAPPED`` when a line has a
    service or VAT code without a mapping (all of them are named)."""
    missing_accounts: list[str] = []
    missing_taxes: list[str] = []
    lines: list[BookkeepingLine] = []
    for line in data.lines:
        account = mappings.accounts.get(line.service_id)
        tax = mappings.taxes.get(line.vat_rate_id)
        if account is None:
            label = line.service_label or "lines without a service"
            if label not in missing_accounts:
                missing_accounts.append(label)
        if tax is None:
            label = line.vat_label or "lines without a VAT code"
            if label not in missing_taxes:
                missing_taxes.append(label)
        if account is not None and tax is not None:
            lines.append(
                BookkeepingLine(
                    description=line.description,
                    quantity=line.quantity,
                    unit_amount=line.unit_amount,
                    net_amount=line.net_amount,
                    vat_amount=line.vat_amount,
                    vat_rate_bp=line.vat_rate_bp,
                    account_code=account[0],
                    tax_code=tax[0],
                )
            )
    if missing_accounts or missing_taxes:
        raise ItemFailure(_unmapped_message(missing_accounts, missing_taxes), "UNMAPPED")
    return BookkeepingDocument(
        kind="invoice" if data.kind == "invoice" else "credit_note",
        number=data.number,
        client_code=data.client_code,
        client_name=data.client_name,
        issue_date=data.issue_date,
        due_date=data.due_date,
        currency=data.currency,
        net_total=data.net_total,
        vat_total=data.vat_total,
        gross_total=data.gross_total,
        lines=tuple(lines),
        invoice_number=data.invoice_number,
        reason=data.reason,
        reference=data.reference,
    )


async def send_item(session: AsyncSession, item: IntegrationSyncItem) -> ItemOutcome:
    """The handler of ``send_invoice`` and ``send_credit_note`` items."""
    if item.entity_id is None:
        raise ItemFailure("The item names no document.", "INVALID")
    repo = BookkeepingRepository(session)
    gateway = get_bookkeeping_gateway()
    organization_id = item.organization_id
    if item.operation == "send_invoice":
        data = await repo.invoice(organization_id, item.entity_id)
    else:
        data = await repo.credit_note(organization_id, item.entity_id)
    if data is None:
        return ItemOutcome("skipped", message="The document no longer exists.")
    if data.voided:
        return ItemOutcome(
            "skipped", message=f"Invoice {data.number} was voided before it was sent."
        )
    document = build_document(data, await repo.mappings(organization_id))
    if item.operation == "send_invoice":
        receipt = await gateway.send_invoice(document, idempotency_key=item.idempotency_key)
    else:
        receipt = await gateway.send_credit_note(document, idempotency_key=item.idempotency_key)
    return ItemOutcome("ok", external_ref=receipt.external_ref)
