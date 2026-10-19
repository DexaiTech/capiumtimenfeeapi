"""Payment, payment allocation and client statement queries, scoped to one organization."""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CreditNote, Invoice, Payment, PaymentAllocation
from app.repositories.billing_common import paginate_by_date
from app.utils.pagination import Page, PageParams


@dataclass(frozen=True, slots=True)
class PaymentFilters:
    client_id: uuid.UUID | None = None
    date_from: date | None = None
    date_to: date | None = None
    methods: Sequence[str] | None = None
    # unallocated / part_allocated / allocated / void, or on_account (some money unallocated)
    state: str | None = None
    q: str | None = None


class PaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, payment: Payment) -> Payment:
        self.session.add(payment)
        await self.session.flush()
        await self.session.refresh(payment)
        return payment

    async def get(
        self, organization_id: uuid.UUID, payment_id: uuid.UUID, *, for_update: bool = False
    ) -> Payment | None:
        stmt = (
            select(Payment)
            .where(Payment.organization_id == organization_id, Payment.id == payment_id)
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_page(
        self, organization_id: uuid.UUID, filters: PaymentFilters, params: PageParams
    ) -> Page[Payment]:
        """Newest first by ``(received_on, id)``."""
        stmt = select(Payment).where(Payment.organization_id == organization_id)
        if filters.client_id is not None:
            stmt = stmt.where(Payment.client_id == filters.client_id)
        if filters.date_from is not None:
            stmt = stmt.where(Payment.received_on >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(Payment.received_on <= filters.date_to)
        if filters.methods:
            stmt = stmt.where(Payment.method.in_(filters.methods))
        if filters.state == "reversed":
            stmt = stmt.where(Payment.reversed_at.is_not(None))
        elif filters.state == "void":
            stmt = stmt.where(Payment.voided_at.is_not(None), Payment.reversed_at.is_(None))
        elif filters.state == "unallocated":
            stmt = stmt.where(Payment.voided_at.is_(None), Payment.allocated_total == 0)
        elif filters.state == "part_allocated":
            stmt = stmt.where(
                Payment.voided_at.is_(None),
                Payment.allocated_total > 0,
                Payment.allocated_total < Payment.amount,
            )
        elif filters.state == "on_account":
            stmt = stmt.where(Payment.voided_at.is_(None), Payment.allocated_total < Payment.amount)
        elif filters.state == "allocated":
            stmt = stmt.where(
                Payment.voided_at.is_(None), Payment.allocated_total == Payment.amount
            )
        if filters.q:
            stmt = stmt.where(
                or_(
                    Payment.reference.icontains(filters.q, autoescape=True),
                    Payment.notes.icontains(filters.q, autoescape=True),
                )
            )
        return await paginate_by_date(
            self.session,
            stmt,
            sort_column=Payment.received_on,
            id_column=Payment.id,
            params=params,
        )

    # --- Allocations -------------------------------------------------------------------

    async def allocations(
        self, organization_id: uuid.UUID, payment_id: uuid.UUID
    ) -> list[tuple[PaymentAllocation, Invoice]]:
        stmt = (
            select(PaymentAllocation, Invoice)
            .join(
                Invoice,
                and_(
                    Invoice.organization_id == PaymentAllocation.organization_id,
                    Invoice.id == PaymentAllocation.invoice_id,
                ),
            )
            .where(
                PaymentAllocation.organization_id == organization_id,
                PaymentAllocation.payment_id == payment_id,
            )
            .order_by(Invoice.issue_date, Invoice.number, PaymentAllocation.id)
            .execution_options(populate_existing=True)
        )
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def get_allocation(
        self, organization_id: uuid.UUID, payment_id: uuid.UUID, allocation_id: uuid.UUID
    ) -> PaymentAllocation | None:
        stmt = (
            select(PaymentAllocation)
            .where(
                PaymentAllocation.organization_id == organization_id,
                PaymentAllocation.payment_id == payment_id,
                PaymentAllocation.id == allocation_id,
            )
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def allocations_by_invoice(
        self, organization_id: uuid.UUID, payment_id: uuid.UUID, invoice_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, PaymentAllocation]:
        wanted = set(invoice_ids)
        if not wanted:
            return {}
        stmt = (
            select(PaymentAllocation)
            .where(
                PaymentAllocation.organization_id == organization_id,
                PaymentAllocation.payment_id == payment_id,
                PaymentAllocation.invoice_id.in_(wanted),
            )
            .execution_options(populate_existing=True)
        )
        return {a.invoice_id: a for a in (await self.session.execute(stmt)).scalars()}

    async def add_allocation(self, allocation: PaymentAllocation) -> PaymentAllocation:
        self.session.add(allocation)
        await self.session.flush()
        return allocation

    async def delete_allocation(self, allocation: PaymentAllocation) -> None:
        await self.session.delete(allocation)
        await self.session.flush()

    # --- Client statement --------------------------------------------------------------

    async def statement_invoices(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, date_to: date
    ) -> list[Invoice]:
        """Issued (not void) invoices up to ``date_to``, in date order."""
        stmt = (
            select(Invoice)
            .where(
                Invoice.organization_id == organization_id,
                Invoice.client_id == client_id,
                Invoice.status == "issued",
                Invoice.issue_date <= date_to,
            )
            .order_by(Invoice.issue_date, Invoice.number)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def statement_credit_notes(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, date_to: date
    ) -> list[CreditNote]:
        stmt = (
            select(CreditNote)
            .where(
                CreditNote.organization_id == organization_id,
                CreditNote.client_id == client_id,
                CreditNote.issue_date <= date_to,
            )
            .order_by(CreditNote.issue_date, CreditNote.number)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def statement_payments(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, date_to: date
    ) -> list[Payment]:
        """Payments received up to ``date_to``: live ones and reversed ones (which the statement
        shows together with their reversal), never plainly void ones."""
        stmt = (
            select(Payment)
            .where(
                Payment.organization_id == organization_id,
                Payment.client_id == client_id,
                or_(Payment.voided_at.is_(None), Payment.reversed_at.is_not(None)),
                Payment.received_on <= date_to,
            )
            .order_by(Payment.received_on, Payment.created_at, Payment.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def unallocated_total(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, date_to: date | None = None
    ) -> int:
        stmt = select(func.coalesce(func.sum(Payment.amount - Payment.allocated_total), 0)).where(
            Payment.organization_id == organization_id,
            Payment.client_id == client_id,
            Payment.voided_at.is_(None),
        )
        if date_to is not None:
            stmt = stmt.where(Payment.received_on <= date_to)
        return int((await self.session.execute(stmt)).scalar_one())
