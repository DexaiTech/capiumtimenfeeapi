"""Queries behind the bookkeeping integration: finding issued documents that have not been queued,
reading a document with its lines, and the service and VAT mappings. Every method is scoped to one
organization. Read-only on billing: the integration never writes to invoices or credit notes."""

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import CursorResult, TextClause, and_, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BookkeepingServiceMapping,
    BookkeepingVatMapping,
    Client,
    CreditNote,
    CreditNoteLine,
    Invoice,
    InvoiceLine,
    Service,
    VatRate,
)

# Queue every issued invoice (and every credit note) that has no sync item yet. One statement, so
# two scanners at the same moment queue each document once (the unique key is the backstop).
_SCAN_INVOICES = text(
    """
    INSERT INTO integration_sync_items
        (id, organization_id, integration, entity_type, entity_id, direction, operation,
         idempotency_key, status, attempts, next_attempt_at, payload)
    SELECT gen_random_uuid(), i.organization_id, 'bookkeeping', 'invoice', i.id, 'outbound',
           'send_invoice', 'invoice:' || CAST(i.id AS text), 'pending', 0, now(),
           jsonb_build_object('label', i.number, 'client_name', c.name,
                              'amount', i.gross_total)
    FROM invoices i
    JOIN clients c ON c.organization_id = i.organization_id AND c.id = i.client_id
    WHERE i.organization_id = :org AND i.status = 'issued'
      AND (CAST(:send_from AS date) IS NULL OR i.issue_date >= CAST(:send_from AS date))
      AND NOT EXISTS (
          SELECT 1 FROM integration_sync_items s
          WHERE s.organization_id = i.organization_id AND s.integration = 'bookkeeping'
            AND s.idempotency_key = 'invoice:' || CAST(i.id AS text))
    ORDER BY i.issued_at, i.id
    LIMIT :limit
    ON CONFLICT (organization_id, integration, idempotency_key) DO NOTHING
    """
)
_SCAN_CREDIT_NOTES = text(
    """
    INSERT INTO integration_sync_items
        (id, organization_id, integration, entity_type, entity_id, direction, operation,
         idempotency_key, status, attempts, next_attempt_at, payload)
    SELECT gen_random_uuid(), n.organization_id, 'bookkeeping', 'credit_note', n.id, 'outbound',
           'send_credit_note', 'credit_note:' || CAST(n.id AS text), 'pending', 0, now(),
           jsonb_build_object('label', n.number, 'client_name', c.name,
                              'invoice_number', i.number, 'amount', n.gross_total)
    FROM credit_notes n
    JOIN clients c ON c.organization_id = n.organization_id AND c.id = n.client_id
    JOIN invoices i ON i.organization_id = n.organization_id AND i.id = n.invoice_id
    WHERE n.organization_id = :org
      AND (CAST(:send_from AS date) IS NULL OR n.issue_date >= CAST(:send_from AS date))
      AND NOT EXISTS (
          SELECT 1 FROM integration_sync_items s
          WHERE s.organization_id = n.organization_id AND s.integration = 'bookkeeping'
            AND s.idempotency_key = 'credit_note:' || CAST(n.id AS text))
    ORDER BY n.created_at, n.id
    LIMIT :limit
    ON CONFLICT (organization_id, integration, idempotency_key) DO NOTHING
    """
)


@dataclass(frozen=True, slots=True)
class LineData:
    description: str
    quantity: Decimal
    unit_amount: int
    net_amount: int
    vat_amount: int
    vat_rate_bp: int
    service_id: uuid.UUID | None
    service_label: str | None
    vat_rate_id: uuid.UUID | None
    vat_label: str | None


@dataclass(frozen=True, slots=True)
class DocumentData:
    kind: str
    number: str
    voided: bool
    client_code: str
    client_name: str
    issue_date: date
    due_date: date | None
    currency: str
    net_total: int
    vat_total: int
    gross_total: int
    reference: str | None
    invoice_number: str | None
    reason: str | None
    lines: tuple[LineData, ...]


@dataclass(frozen=True, slots=True)
class Mappings:
    """Account and tax codes by service and VAT code; the key ``None`` is the code for lines that
    have no service (no VAT code)."""

    accounts: dict[uuid.UUID | None, tuple[str, str | None]]
    taxes: dict[uuid.UUID | None, tuple[str, str | None]]


class BookkeepingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Finding what to send ---------------------------------------------------------------

    async def queue_invoices(
        self, organization_id: uuid.UUID, send_from: date | None, limit: int
    ) -> int:
        return await self._scan(_SCAN_INVOICES, organization_id, send_from, limit)

    async def queue_credit_notes(
        self, organization_id: uuid.UUID, send_from: date | None, limit: int
    ) -> int:
        return await self._scan(_SCAN_CREDIT_NOTES, organization_id, send_from, limit)

    async def _scan(
        self,
        statement: TextClause,
        organization_id: uuid.UUID,
        send_from: date | None,
        limit: int,
    ) -> int:
        result = await self.session.execute(
            statement, {"org": organization_id, "send_from": send_from, "limit": limit}
        )
        assert isinstance(result, CursorResult)
        return max(result.rowcount, 0)

    # --- Reading a document -------------------------------------------------------------------

    async def invoice(
        self, organization_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> DocumentData | None:
        row = (
            await self.session.execute(
                select(Invoice, Client.code, Client.name)
                .join(
                    Client,
                    and_(
                        Client.organization_id == Invoice.organization_id,
                        Client.id == Invoice.client_id,
                    ),
                )
                .where(Invoice.organization_id == organization_id, Invoice.id == invoice_id)
            )
        ).first()
        if row is None:
            return None
        invoice, client_code, client_name = row
        lines = (
            await self.session.execute(
                select(InvoiceLine, Service.code, Service.name, VatRate.name, VatRate.rate_bp)
                .outerjoin(
                    Service,
                    and_(
                        Service.organization_id == InvoiceLine.organization_id,
                        Service.id == InvoiceLine.service_id,
                    ),
                )
                .outerjoin(
                    VatRate,
                    and_(
                        VatRate.organization_id == InvoiceLine.organization_id,
                        VatRate.id == InvoiceLine.vat_rate_id,
                    ),
                )
                .where(
                    InvoiceLine.organization_id == organization_id,
                    InvoiceLine.invoice_id == invoice_id,
                )
                .order_by(InvoiceLine.position)
            )
        ).all()
        return DocumentData(
            kind="invoice",
            number=invoice.number or "",
            voided=invoice.status != "issued",
            client_code=client_code,
            client_name=client_name,
            issue_date=invoice.issue_date or date.min,
            due_date=invoice.due_date,
            currency=invoice.currency,
            net_total=invoice.net_total,
            vat_total=invoice.vat_total,
            gross_total=invoice.gross_total,
            reference=invoice.reference,
            invoice_number=None,
            reason=None,
            lines=tuple(
                LineData(
                    description=line.description,
                    quantity=line.quantity,
                    unit_amount=line.unit_amount,
                    net_amount=line.net_amount,
                    vat_amount=line.vat_amount,
                    vat_rate_bp=line.vat_rate_bp,
                    service_id=line.service_id,
                    service_label=f"{code} {name}" if code else None,
                    vat_rate_id=line.vat_rate_id,
                    vat_label=f"{vat_name} ({bp / 100:.2f}%)" if vat_name else None,
                )
                for line, code, name, vat_name, bp in lines
            ),
        )

    async def credit_note(
        self, organization_id: uuid.UUID, credit_note_id: uuid.UUID
    ) -> DocumentData | None:
        row = (
            await self.session.execute(
                select(CreditNote, Client.code, Client.name, Invoice.number, Invoice.currency)
                .join(
                    Client,
                    and_(
                        Client.organization_id == CreditNote.organization_id,
                        Client.id == CreditNote.client_id,
                    ),
                )
                .join(
                    Invoice,
                    and_(
                        Invoice.organization_id == CreditNote.organization_id,
                        Invoice.id == CreditNote.invoice_id,
                    ),
                )
                .where(
                    CreditNote.organization_id == organization_id,
                    CreditNote.id == credit_note_id,
                )
            )
        ).first()
        if row is None:
            return None
        note, client_code, client_name, invoice_number, currency = row
        lines = (
            await self.session.execute(
                select(CreditNoteLine, VatRate.name, VatRate.rate_bp)
                .outerjoin(
                    VatRate,
                    and_(
                        VatRate.organization_id == CreditNoteLine.organization_id,
                        VatRate.id == CreditNoteLine.vat_rate_id,
                    ),
                )
                .where(
                    CreditNoteLine.organization_id == organization_id,
                    CreditNoteLine.credit_note_id == credit_note_id,
                )
                .order_by(CreditNoteLine.position)
            )
        ).all()
        return DocumentData(
            kind="credit_note",
            number=note.number,
            voided=False,
            client_code=client_code,
            client_name=client_name,
            issue_date=note.issue_date,
            due_date=None,
            currency=currency,
            net_total=note.net_total,
            vat_total=note.vat_total,
            gross_total=note.gross_total,
            reference=None,
            invoice_number=invoice_number,
            reason=note.reason,
            lines=tuple(
                LineData(
                    description=line.description,
                    quantity=Decimal(1),
                    unit_amount=line.net_amount,
                    net_amount=line.net_amount,
                    vat_amount=line.vat_amount,
                    vat_rate_bp=line.vat_rate_bp,
                    service_id=None,
                    service_label=None,
                    vat_rate_id=line.vat_rate_id,
                    vat_label=f"{vat_name} ({bp / 100:.2f}%)" if vat_name else None,
                )
                for line, vat_name, bp in lines
            ),
        )

    # --- Mappings -----------------------------------------------------------------------------

    async def mappings(self, organization_id: uuid.UUID) -> Mappings:
        accounts = (
            await self.session.execute(
                select(BookkeepingServiceMapping).where(
                    BookkeepingServiceMapping.organization_id == organization_id
                )
            )
        ).scalars()
        taxes = (
            await self.session.execute(
                select(BookkeepingVatMapping).where(
                    BookkeepingVatMapping.organization_id == organization_id
                )
            )
        ).scalars()
        return Mappings(
            accounts={m.service_id: (m.account_code, m.account_name) for m in accounts},
            taxes={m.vat_rate_id: (m.tax_code, m.tax_name) for m in taxes},
        )

    async def services(self, organization_id: uuid.UUID) -> list[Service]:
        stmt = (
            select(Service)
            .where(Service.organization_id == organization_id)
            .order_by(Service.sort_order, Service.code)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def vat_rates(self, organization_id: uuid.UUID) -> list[VatRate]:
        stmt = (
            select(VatRate)
            .where(VatRate.organization_id == organization_id)
            .order_by(VatRate.archived_at.is_not(None), VatRate.rate_bp.desc(), VatRate.name)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def replace_mappings(
        self,
        organization_id: uuid.UUID,
        accounts: list[BookkeepingServiceMapping],
        taxes: list[BookkeepingVatMapping],
    ) -> None:
        """Replace the practice's mappings with exactly these."""
        await self.session.execute(
            delete(BookkeepingServiceMapping).where(
                BookkeepingServiceMapping.organization_id == organization_id
            )
        )
        await self.session.execute(
            delete(BookkeepingVatMapping).where(
                BookkeepingVatMapping.organization_id == organization_id
            )
        )
        self.session.add_all([*accounts, *taxes])
        await self.session.flush()
