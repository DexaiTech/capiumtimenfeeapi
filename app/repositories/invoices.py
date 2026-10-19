"""Invoice and invoice line queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, false, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Client,
    CreditNote,
    Invoice,
    InvoiceApproval,
    InvoiceLine,
    Payment,
    PaymentAllocation,
)
from app.utils.pagination import Page, PageParams, paginate

# The first six are the original statuses (the client overview counts every one of them); the
# approval statuses and ``credited`` were added later.
DISPLAY_STATUSES = (
    "draft",
    "issued",
    "void",
    "overdue",
    "part_paid",
    "paid",
    "awaiting_approval",
    "approved",
    "credited",
)


@dataclass(frozen=True, slots=True)
class InvoiceFilters:
    client_id: uuid.UUID | None = None
    statuses: Sequence[str] | None = None
    date_from: date | None = None
    date_to: date | None = None
    q: str | None = None
    outstanding: bool | None = None


@dataclass(frozen=True, slots=True)
class InvoiceSummaryFigures:
    total: int
    by_status: dict[str, int]
    outstanding: int
    overdue: int
    awaiting_approval_gross: int


def balance_expr() -> ColumnElement[int]:
    return Invoice.gross_total - Invoice.paid_total - Invoice.credited_total


def fully_credited() -> ColumnElement[bool]:
    return and_(Invoice.credited_total > 0, Invoice.credited_total == Invoice.gross_total)


def display_status_condition(status: str, today: date) -> ColumnElement[bool]:
    """SQL for the derived display status (see ``services.invoices.display_status``)."""
    issued = Invoice.status == "issued"
    owing = balance_expr() > 0
    overdue = and_(issued, owing, Invoice.due_date < today)
    match status:
        case "draft" | "awaiting_approval" | "approved" | "void":
            return Invoice.status == status
        case "credited":
            return and_(issued, fully_credited())
        case "paid":
            return and_(issued, balance_expr() == 0, not_(fully_credited()))
        case "overdue":
            return overdue
        case "part_paid":
            return and_(issued, owing, Invoice.paid_total > 0, Invoice.due_date >= today)
        case "issued":
            return and_(issued, owing, Invoice.paid_total == 0, Invoice.due_date >= today)
    return false()


def _apply_common_filters(
    stmt: Select[*tuple[Any, ...]], filters: InvoiceFilters
) -> Select[*tuple[Any, ...]]:
    """Client, issue-date range and search: the filters the list and its summary share."""
    if filters.client_id is not None:
        stmt = stmt.where(Invoice.client_id == filters.client_id)
    if filters.date_from is not None:
        stmt = stmt.where(Invoice.issue_date >= filters.date_from)
    if filters.date_to is not None:
        stmt = stmt.where(Invoice.issue_date <= filters.date_to)
    if filters.q:
        client_match = (
            select(Client.id)
            .where(
                Client.organization_id == Invoice.organization_id,
                Client.id == Invoice.client_id,
                or_(
                    Client.name.icontains(filters.q, autoescape=True),
                    Client.code.icontains(filters.q, autoescape=True),
                ),
            )
            .exists()
        )
        stmt = stmt.where(or_(Invoice.number.icontains(filters.q, autoescape=True), client_match))
    return stmt


class InvoiceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, invoice: Invoice) -> Invoice:
        self.session.add(invoice)
        await self.session.flush()
        await self.session.refresh(invoice)
        return invoice

    async def get(
        self, organization_id: uuid.UUID, invoice_id: uuid.UUID, *, for_update: bool = False
    ) -> Invoice | None:
        stmt = (
            select(Invoice)
            .where(Invoice.organization_id == organization_id, Invoice.id == invoice_id)
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_many(
        self, organization_id: uuid.UUID, ids: Iterable[uuid.UUID]
    ) -> list[Invoice]:
        """Lock invoices in id order (the deadlock-free order every writer uses)."""
        wanted = sorted(set(ids))
        if not wanted:
            return []
        stmt = (
            select(Invoice)
            .where(Invoice.organization_id == organization_id, Invoice.id.in_(wanted))
            .order_by(Invoice.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def list_page(
        self,
        organization_id: uuid.UUID,
        filters: InvoiceFilters,
        *,
        today: date,
        params: PageParams,
    ) -> Page[Invoice]:
        """Newest first by ``(created_at, id)``."""
        stmt = select(Invoice).where(Invoice.organization_id == organization_id)
        stmt = _apply_common_filters(stmt, filters)
        if filters.statuses:
            stmt = stmt.where(or_(*(display_status_condition(s, today) for s in filters.statuses)))
        if filters.outstanding is True:
            stmt = stmt.where(Invoice.status == "issued", balance_expr() > 0)
        elif filters.outstanding is False:
            stmt = stmt.where(or_(Invoice.status != "issued", balance_expr() == 0))
        return await paginate(
            self.session,
            stmt,
            sort_column=Invoice.created_at,
            id_column=Invoice.id,
            params=params,
            descending=True,
        )

    async def summary(
        self, organization_id: uuid.UUID, filters: InvoiceFilters, *, today: date
    ) -> InvoiceSummaryFigures:
        """Counts per display status and the money still owed, for the invoices matching the
        client, date and search filters (the status filter is ignored)."""
        issued_owing = and_(Invoice.status == "issued", balance_expr() > 0)
        stmt = select(
            func.count(),
            *(
                func.count().filter(display_status_condition(status, today))
                for status in DISPLAY_STATUSES
            ),
            func.coalesce(func.sum(balance_expr()).filter(issued_owing), 0),
            func.coalesce(
                func.sum(balance_expr()).filter(display_status_condition("overdue", today)), 0
            ),
            func.coalesce(
                func.sum(Invoice.gross_total).filter(Invoice.status == "awaiting_approval"), 0
            ),
        ).where(Invoice.organization_id == organization_id)
        row = (await self.session.execute(_apply_common_filters(stmt, filters))).one()
        size = len(DISPLAY_STATUSES)
        return InvoiceSummaryFigures(
            total=int(row[0]),
            by_status={name: int(row[1 + i]) for i, name in enumerate(DISPLAY_STATUSES)},
            outstanding=int(row[1 + size]),
            overdue=int(row[2 + size]),
            awaiting_approval_gross=int(row[3 + size]),
        )

    async def open_for_client(
        self, organization_id: uuid.UUID, client_id: uuid.UUID
    ) -> list[uuid.UUID]:
        """Ids of the client's issued invoices with a balance, oldest first."""
        stmt = (
            select(Invoice.id)
            .where(
                Invoice.organization_id == organization_id,
                Invoice.client_id == client_id,
                Invoice.status == "issued",
                balance_expr() > 0,
            )
            .order_by(Invoice.issue_date, Invoice.number, Invoice.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    # --- Lines -------------------------------------------------------------------------

    async def lines(self, organization_id: uuid.UUID, invoice_id: uuid.UUID) -> list[InvoiceLine]:
        stmt = (
            select(InvoiceLine)
            .where(
                InvoiceLine.organization_id == organization_id,
                InvoiceLine.invoice_id == invoice_id,
            )
            .order_by(InvoiceLine.position, InvoiceLine.created_at, InvoiceLine.id)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get_line(
        self, organization_id: uuid.UUID, invoice_id: uuid.UUID, line_id: uuid.UUID
    ) -> InvoiceLine | None:
        stmt = (
            select(InvoiceLine)
            .where(
                InvoiceLine.organization_id == organization_id,
                InvoiceLine.invoice_id == invoice_id,
                InvoiceLine.id == line_id,
            )
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_lines(self, lines: Sequence[InvoiceLine]) -> None:
        self.session.add_all(lines)
        await self.session.flush()
        for line in lines:
            await self.session.refresh(line)

    async def delete_line(self, line: InvoiceLine) -> None:
        await self.session.delete(line)
        await self.session.flush()

    async def delete(self, invoice: Invoice) -> None:
        await self.session.delete(invoice)
        await self.session.flush()

    async def max_position(self, organization_id: uuid.UUID, invoice_id: uuid.UUID) -> int:
        stmt = select(func.coalesce(func.max(InvoiceLine.position), 0)).where(
            InvoiceLine.organization_id == organization_id, InvoiceLine.invoice_id == invoice_id
        )
        return int((await self.session.execute(stmt)).scalar_one())

    # --- Settlement --------------------------------------------------------------------

    async def payment_allocations(
        self, organization_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> list[tuple[PaymentAllocation, Payment]]:
        stmt = (
            select(PaymentAllocation, Payment)
            .join(
                Payment,
                and_(
                    Payment.organization_id == PaymentAllocation.organization_id,
                    Payment.id == PaymentAllocation.payment_id,
                ),
            )
            .where(
                PaymentAllocation.organization_id == organization_id,
                PaymentAllocation.invoice_id == invoice_id,
            )
            .order_by(Payment.received_on, PaymentAllocation.created_at, PaymentAllocation.id)
        )
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def credit_notes(
        self, organization_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> list[CreditNote]:
        stmt = (
            select(CreditNote)
            .where(
                CreditNote.organization_id == organization_id, CreditNote.invoice_id == invoice_id
            )
            .order_by(CreditNote.issue_date, CreditNote.number)
        )
        return list((await self.session.execute(stmt)).scalars())

    # --- Approval ----------------------------------------------------------------------

    async def add_approval(self, approval: InvoiceApproval) -> InvoiceApproval:
        self.session.add(approval)
        await self.session.flush()
        await self.session.refresh(approval)
        return approval

    async def approvals(
        self, organization_id: uuid.UUID, invoice_id: uuid.UUID
    ) -> list[InvoiceApproval]:
        """The approval history, oldest first."""
        stmt = (
            select(InvoiceApproval)
            .where(
                InvoiceApproval.organization_id == organization_id,
                InvoiceApproval.invoice_id == invoice_id,
            )
            .order_by(InvoiceApproval.created_at, InvoiceApproval.id)
        )
        return list((await self.session.execute(stmt)).scalars())
