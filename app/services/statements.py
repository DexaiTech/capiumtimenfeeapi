"""The client statement: invoices, credit notes and payments in date order with a running
balance.

Invoices add to the balance (gross), credit notes and payments reduce it. Void invoices
and void payments are left out. A reversed payment (a bounced cheque) is shown, then its
reversal, which adds the money back on the day it was reversed. Money received but not
allocated (on account) is still a payment, so it already reduces the balance;
``unallocated_cash`` says how much of the closing balance is such cash.

The statement is built from a client and an organization rather than a request context, so
the worker that emails statements builds the very same document (``render``).
"""

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.db.models import Client, Organization
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.payments import PaymentRepository
from app.schemas.billing import ClientRef
from app.schemas.payments import StatementEntry, StatementRead
from app.services import invoice_pdf
from app.services.billing_common import get_client_or_404, local_date, today_for
from app.services.document_templates import pdf_template
from app.services.invoices import addressee_for
from app.services.policy import OrgContext

_ORDER = {"invoice": 0, "credit_note": 1, "payment": 2, "payment_reversal": 3}
_METHODS = {
    "bank_transfer": "Bank transfer",
    "card": "Card",
    "direct_debit": "Direct debit",
    "cash": "Cash",
    "cheque": "Cheque",
    "other": "Payment",
}


@dataclass(slots=True)
class _Movement:
    day: date
    kind: str
    id: uuid.UUID
    number: str | None
    description: str
    amount: int
    tiebreak: str


class StatementService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.payments = PaymentRepository(session)
        self.refs = BillingReferenceRepository(session)

    async def statement(
        self,
        ctx: OrgContext,
        client_id: uuid.UUID,
        date_from: date | None,
        date_to: date | None,
    ) -> StatementRead:
        client = await get_client_or_404(self.refs, ctx.organization_id, client_id)
        return await self.build(ctx.organization, client, date_from, date_to)

    async def build(
        self,
        organization: Organization,
        client: Client,
        date_from: date | None,
        date_to: date | None,
    ) -> StatementRead:
        org_id = organization.id
        end = date_to or today_for(organization)
        if date_from is not None and date_from > end:
            raise ValidationError.for_field("date_from", "date_from is after date_to.")

        movements: list[_Movement] = []
        for invoice in await self.payments.statement_invoices(org_id, client.id, end):
            assert invoice.issue_date is not None
            due = f", due {invoice_pdf.long_date(invoice.due_date)}" if invoice.due_date else ""
            movements.append(
                _Movement(
                    invoice.issue_date,
                    "invoice",
                    invoice.id,
                    invoice.number,
                    f"Invoice{due}",
                    invoice.gross_total,
                    invoice.number or "",
                )
            )
        for note in await self.payments.statement_credit_notes(org_id, client.id, end):
            movements.append(
                _Movement(
                    note.issue_date,
                    "credit_note",
                    note.id,
                    note.number,
                    f"Credit note: {note.reason}",
                    -note.gross_total,
                    note.number,
                )
            )
        for payment in await self.payments.statement_payments(org_id, client.id, end):
            label = _METHODS.get(payment.method, "Payment")
            ref = f" ({payment.reference})" if payment.reference else ""
            movements.append(
                _Movement(
                    payment.received_on,
                    "payment",
                    payment.id,
                    payment.reference,
                    f"{label} received{ref}",
                    -payment.amount,
                    payment.created_at.isoformat(),
                )
            )
            if payment.reversed_at is not None:
                # The reason stays inside the practice: the statement goes to the client.
                day = max(local_date(organization, payment.reversed_at), payment.received_on)
                if day <= end:
                    what = "Payment returned" if payment.bounced else "Payment reversed"
                    movements.append(
                        _Movement(
                            day,
                            "payment_reversal",
                            payment.id,
                            payment.reference,
                            f"{what}{ref}",
                            payment.amount,
                            payment.reversed_at.isoformat(),
                        )
                    )
        movements.sort(key=lambda m: (m.day, _ORDER[m.kind], m.tiebreak, m.id))

        opening = sum(m.amount for m in movements if date_from and m.day < date_from)
        balance = opening
        entries: list[StatementEntry] = []
        invoiced = credited = paid = 0
        for movement in movements:
            if date_from and movement.day < date_from:
                continue
            balance += movement.amount
            if movement.kind == "invoice":
                invoiced += movement.amount
            elif movement.kind == "credit_note":
                credited -= movement.amount
            else:
                # Payments add to what was paid, reversals take it back.
                paid -= movement.amount
            entries.append(
                StatementEntry(
                    date=movement.day,
                    type=movement.kind,
                    id=movement.id,
                    number=movement.number,
                    description=movement.description,
                    amount=movement.amount,
                    balance=balance,
                )
            )
        return StatementRead(
            client=ClientRef.from_model(client),
            currency=organization.currency,
            date_from=date_from,
            date_to=end,
            opening_balance=opening,
            entries=entries,
            invoiced=invoiced,
            credited=credited,
            paid=paid,
            closing_balance=balance,
            unallocated_cash=await self.payments.unallocated_total(org_id, client.id, end),
        )

    async def pdf(
        self,
        ctx: OrgContext,
        client_id: uuid.UUID,
        date_from: date | None,
        date_to: date | None,
    ) -> tuple[str, bytes]:
        client = await get_client_or_404(self.refs, ctx.organization_id, client_id)
        statement = await self.build(ctx.organization, client, date_from, date_to)
        return await self.render(ctx.organization, client, statement)

    async def render(
        self, organization: Organization, client: Client, statement: StatementRead
    ) -> tuple[str, bytes]:
        """The PDF of a built statement, drawn with the client's invoice template (else the
        practice default: letterhead, bank details and footer), and its file name."""
        data = invoice_pdf.StatementData(
            client=client,
            currency=statement.currency,
            date_from=statement.date_from,
            date_to=statement.date_to,
            opening_balance=statement.opening_balance,
            closing_balance=statement.closing_balance,
            unallocated_cash=statement.unallocated_cash,
            entries=[
                invoice_pdf.StatementLine(
                    day=e.date,
                    kind=e.type,
                    number=e.number,
                    description=e.description,
                    amount=e.amount,
                    balance=e.balance,
                )
                for e in statement.entries
            ],
        )
        content = invoice_pdf.render_statement(
            organization=organization,
            addressee=await addressee_for(self.refs, organization.id, client),
            data=data,
            template=await pdf_template(self.session, organization.id, client),
        )
        return f"statement-{client.code}-{statement.date_to.isoformat()}.pdf", content
