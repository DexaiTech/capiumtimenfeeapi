"""Credit notes: reduce what is owed on one issued invoice.

Issued (numbered) on creation and immutable afterwards. The gross total may not exceed the
invoice's outstanding balance, checked and applied under the invoice row lock. A credit
note does not reinstate WIP.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError, ValidationError
from app.db.models import CreditNote, CreditNoteLine
from app.db.session import unit_of_work
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.credit_notes import CreditNoteFilters, CreditNoteRepository
from app.repositories.invoices import InvoiceRepository
from app.schemas.invoices import CreditNoteCreate, CreditNoteRead
from app.services import document_templates, invoice_pdf
from app.services.billing_common import VatResolver, conflict, get_client_or_404, record, today_for
from app.services.invoices import addressee_for
from app.services.policy import OrgContext
from app.services.sequences import next_number
from app.utils.ids import new_id
from app.utils.money import vat_on
from app.utils.pagination import Page, PageParams


class CreditNoteService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.notes = CreditNoteRepository(session)
        self.invoices = InvoiceRepository(session)
        self.refs = BillingReferenceRepository(session)

    async def create(
        self,
        ctx: OrgContext,
        invoice_id: uuid.UUID,
        data: CreditNoteCreate,
        rctx: RequestContext,
    ) -> CreditNoteRead:
        async with unit_of_work(self.session):
            invoice = await self.invoices.get(ctx.organization_id, invoice_id, for_update=True)
            if invoice is None:
                raise NotFoundError("Invoice not found.")
            if invoice.status != "issued":
                raise conflict(
                    "INVOICE_NOT_ISSUED", "Credit notes can be raised on issued invoices only."
                )
            assert invoice.issue_date is not None
            issue_date = data.issue_date or today_for(ctx.organization)
            if issue_date < invoice.issue_date:
                raise ValidationError.for_field(
                    "issue_date", "A credit note cannot be dated before its invoice."
                )
            client = await get_client_or_404(self.refs, ctx.organization_id, invoice.client_id)
            resolver = VatResolver(self.refs, ctx.organization)
            note_id = new_id()
            lines: list[CreditNoteLine] = []
            for position, item in enumerate(data.lines, start=1):
                rate_id, rate_bp = await resolver.resolve(
                    client=client, line_rate_id=item.vat_rate_id, validate_line_rate=True
                )
                lines.append(
                    CreditNoteLine(
                        organization_id=ctx.organization_id,
                        credit_note_id=note_id,
                        position=position,
                        description=item.description,
                        net_amount=item.net_amount,
                        vat_rate_id=rate_id,
                        vat_rate_bp=rate_bp,
                        vat_amount=vat_on(item.net_amount, rate_bp),
                    )
                )
            net = sum(line.net_amount for line in lines)
            vat = sum(line.vat_amount for line in lines)
            gross = net + vat
            if gross > invoice.balance:
                raise conflict(
                    "CREDIT_EXCEEDS_BALANCE",
                    "The credit note is more than the invoice's outstanding balance.",
                    details={"gross_total": gross, "balance": invoice.balance},
                )
            note = await self.notes.add(
                CreditNote(
                    id=note_id,
                    organization_id=ctx.organization_id,
                    client_id=client.id,
                    invoice_id=invoice.id,
                    number=await next_number(self.session, ctx.organization_id, "credit_note"),
                    issue_date=issue_date,
                    reason=data.reason,
                    net_total=net,
                    vat_total=vat,
                    gross_total=gross,
                    created_by=ctx.user.id,
                ),
                lines,
            )
            invoice.credited_total += gross
            await self.session.flush()
            await record(
                self.session,
                ctx,
                rctx,
                "credit_note.issued",
                target_type="credit_note",
                target_id=note.id,
                meta={
                    "number": note.number,
                    "invoice_id": str(invoice.id),
                    "invoice_number": invoice.number,
                    "client_id": str(client.id),
                    "client_name": client.name,
                    "net_total": net,
                    "vat_total": vat,
                    "gross_total": gross,
                    "reason": data.reason,
                    "invoice_balance": invoice.balance,
                },
            )
            saved_lines = await self.notes.lines(ctx.organization_id, note.id)
        return CreditNoteRead.build(
            note, client=client, invoice_number=invoice.number, lines=saved_lines
        )

    async def list_credit_notes(
        self, ctx: OrgContext, filters: CreditNoteFilters, params: PageParams
    ) -> tuple[Page[CreditNote], list[CreditNoteRead]]:
        page = await self.notes.list_page(ctx.organization_id, filters, params)
        clients = await self.refs.clients_by_ids(
            ctx.organization_id, [n.client_id for n in page.items]
        )
        numbers = await self.notes.invoice_numbers(
            ctx.organization_id, [n.invoice_id for n in page.items]
        )
        reads = [
            CreditNoteRead.build(
                n,
                client=clients.get(n.client_id),
                invoice_number=numbers.get(n.invoice_id),
                lines=None,
            )
            for n in page.items
        ]
        return page, reads

    async def get(self, ctx: OrgContext, credit_note_id: uuid.UUID) -> CreditNoteRead:
        note = await self._get(ctx, credit_note_id)
        invoice = await self.invoices.get(ctx.organization_id, note.invoice_id)
        client = await self.refs.get_client(ctx.organization_id, note.client_id)
        lines = await self.notes.lines(ctx.organization_id, note.id)
        return CreditNoteRead.build(
            note, client=client, invoice_number=invoice.number if invoice else None, lines=lines
        )

    async def pdf(self, ctx: OrgContext, credit_note_id: uuid.UUID) -> tuple[str, bytes]:
        note = await self._get(ctx, credit_note_id)
        invoice = await self.invoices.get(ctx.organization_id, note.invoice_id)
        client = await self.refs.get_client(ctx.organization_id, note.client_id)
        if invoice is None or client is None:
            raise NotFoundError("Credit note not found.")
        content = invoice_pdf.render_credit_note(
            organization=ctx.organization,
            client=client,
            addressee=await addressee_for(self.refs, ctx.organization_id, client),
            note=note,
            invoice=invoice,
            lines=await self.notes.lines(ctx.organization_id, note.id),
            template=await document_templates.pdf_template(
                self.session, ctx.organization_id, client
            ),
        )
        return f"{note.number}.pdf", content

    async def _get(self, ctx: OrgContext, credit_note_id: uuid.UUID) -> CreditNote:
        note = await self.notes.get(ctx.organization_id, credit_note_id)
        if note is None:
            raise NotFoundError("Credit note not found.")
        return note
