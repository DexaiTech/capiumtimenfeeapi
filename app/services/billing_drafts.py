"""Pricing lines and raising a draft invoice from them, without WIP.

Shared by estimates (convert to invoice) and recurring schedules (a run). Lines are priced
here: net = round(quantity x unit_amount), VAT resolved the way invoice lines do it (the
chosen rate, else the service's, the client's, the practice's), snapshotted on the line.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from app.core.exceptions import ValidationError
from app.db.models import Client, Invoice, InvoiceLine, Organization
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.invoices import InvoiceRepository
from app.services.billing_common import VatResolver, invalid
from app.utils.ids import new_id
from app.utils.money import line_net, vat_on


class LineLike(Protocol):
    """What ``price_lines`` reads: a request line, or a stored template line."""

    description: str
    quantity: Decimal
    unit_amount: int
    vat_rate_id: uuid.UUID | None
    service_id: uuid.UUID | None
    job_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class PricedLine:
    position: int
    description: str
    job_id: uuid.UUID | None
    service_id: uuid.UUID | None
    quantity: Decimal
    unit_amount: int
    net_amount: int
    vat_rate_id: uuid.UUID | None
    vat_rate_bp: int
    vat_amount: int


async def price_lines(
    refs: BillingReferenceRepository,
    organization: Organization,
    client: Client,
    lines: Sequence[LineLike],
    *,
    field: str = "lines",
) -> list[PricedLine]:
    """Check each line's job and service and price it. ``field`` names the list in errors."""
    resolver = VatResolver(refs, organization)
    priced: list[PricedLine] = []
    for index, line in enumerate(lines):
        where = f"{field}.{index}"
        if line.job_id is not None:
            job = await refs.get_job(organization.id, line.job_id)
            if job is None or job.client_id != client.id:
                raise ValidationError.for_field(
                    f"{where}.job_id", "The job must be one of the client's jobs."
                )
        if line.service_id is not None:
            service = await refs.get_service(organization.id, line.service_id)
            if service is None:
                raise ValidationError.for_field(
                    f"{where}.service_id", "This service does not exist."
                )
        rate_id, rate_bp = await resolver.resolve(
            client=client,
            line_rate_id=line.vat_rate_id,
            service_id=line.service_id,
            validate_line_rate=True,
        )
        net = line_net(line.quantity, line.unit_amount)
        priced.append(
            PricedLine(
                position=index + 1,
                description=line.description,
                job_id=line.job_id,
                service_id=line.service_id,
                quantity=line.quantity,
                unit_amount=line.unit_amount,
                net_amount=net,
                vat_rate_id=rate_id,
                vat_rate_bp=rate_bp,
                vat_amount=vat_on(net, rate_bp),
            )
        )
    return priced


def totals(lines: Sequence[PricedLine]) -> tuple[int, int, int]:
    """``(net, vat, gross)`` of priced lines."""
    net = sum(line.net_amount for line in lines)
    vat = sum(line.vat_amount for line in lines)
    return net, vat, net + vat


async def create_invoice_draft(
    invoices: InvoiceRepository,
    organization: Organization,
    client: Client,
    lines: Sequence[PricedLine],
    *,
    created_by: uuid.UUID | None,
    reference: str | None = None,
    notes: str | None = None,
    issue_date: date | None = None,
) -> Invoice:
    """A draft invoice of manual lines. Caller owns the transaction and the audit entry."""
    net, vat, gross = totals(lines)
    if gross < 0:
        raise invalid("INVOICE_TOTAL_NEGATIVE", "lines", "The invoice total cannot be negative.")
    invoice = await invoices.add(
        Invoice(
            organization_id=organization.id,
            client_id=client.id,
            status="draft",
            issue_date=issue_date,
            reference=reference,
            notes=notes,
            currency=organization.currency,
            net_total=net,
            vat_total=vat,
            gross_total=gross,
            paid_total=0,
            credited_total=0,
            created_by=created_by,
        )
    )
    await invoices.add_lines(
        [
            InvoiceLine(
                id=new_id(),
                organization_id=organization.id,
                invoice_id=invoice.id,
                position=line.position,
                kind="manual",
                description=line.description,
                job_id=line.job_id,
                service_id=line.service_id,
                quantity=line.quantity,
                unit_amount=line.unit_amount,
                net_amount=line.net_amount,
                vat_rate_id=line.vat_rate_id,
                vat_rate_bp=line.vat_rate_bp,
                vat_amount=line.vat_amount,
            )
            for line in lines
        ]
    )
    return invoice
