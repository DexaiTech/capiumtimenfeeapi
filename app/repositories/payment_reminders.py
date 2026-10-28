"""Payment reminders: the log of reminders about overdue invoices, and who is due one."""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Invoice, PaymentReminder
from app.repositories.expense_paging import AnyRow, keyset_page
from app.repositories.invoices import balance_expr
from app.utils.ids import new_id
from app.utils.pagination import PageParams


@dataclass(frozen=True, slots=True)
class ReminderFilters:
    client_id: uuid.UUID | None = None
    invoice_id: uuid.UUID | None = None
    status: str | None = None
    source: str | None = None
    date_from: date | None = None
    date_to: date | None = None


class PaymentReminderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Who is due a reminder ---------------------------------------------------------

    async def overdue_invoices(
        self, organization_id: uuid.UUID, client_ids: Iterable[uuid.UUID], today: date
    ) -> list[Invoice]:
        """The clients' issued invoices that are past their due date and still owe money, by
        client then due date."""
        stmt = (
            select(Invoice)
            .where(
                Invoice.organization_id == organization_id,
                Invoice.client_id.in_(set(client_ids)),
                *_overdue(today),
            )
            .order_by(Invoice.client_id, Invoice.due_date, Invoice.number, Invoice.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def invoices_by_ids(
        self, organization_id: uuid.UUID, ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, Invoice]:
        wanted = set(ids)
        if not wanted:
            return {}
        stmt = select(Invoice).where(
            Invoice.organization_id == organization_id, Invoice.id.in_(wanted)
        )
        return {i.id: i for i in (await self.session.execute(stmt)).scalars()}

    async def scheduled_candidates(
        self, organization_id: uuid.UUID, *, today: date, first_day: int
    ) -> list[Invoice]:
        """Issued invoices that still owe money and whose first reminder day (the due date plus
        ``first_day``, which may be negative) has come."""
        stmt = (
            select(Invoice)
            .where(
                Invoice.organization_id == organization_id,
                Invoice.status == "issued",
                balance_expr() > 0,
                Invoice.due_date.is_not(None),
                Invoice.due_date <= today - timedelta(days=first_day),
            )
            .order_by(Invoice.client_id, Invoice.due_date, Invoice.number, Invoice.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def last_reminded_on(
        self,
        organization_id: uuid.UUID,
        invoice_ids: Iterable[uuid.UUID],
        *,
        stale_before: datetime,
    ) -> dict[uuid.UUID, date]:
        """The latest day each invoice was reminded about: a reminder that was sent, or queued
        and not older than ``stale_before`` (a job that died leaves a queued row behind; it must
        not silence the invoice forever). Manual and scheduled reminders both count."""
        wanted = set(invoice_ids)
        if not wanted:
            return {}
        stmt = (
            select(PaymentReminder.invoice_id, func.max(PaymentReminder.sent_on))
            .where(
                PaymentReminder.organization_id == organization_id,
                PaymentReminder.invoice_id.in_(wanted),
                or_(
                    PaymentReminder.status == "sent",
                    and_(
                        PaymentReminder.status == "queued",
                        PaymentReminder.created_at >= stale_before,
                    ),
                ),
            )
            .group_by(PaymentReminder.invoice_id)
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

    async def reminded_on(
        self, organization_id: uuid.UUID, invoice_ids: Iterable[uuid.UUID], day: date
    ) -> set[uuid.UUID]:
        """The invoices that already have a reminder queued or sent for the day."""
        wanted = set(invoice_ids)
        if not wanted:
            return set()
        stmt = select(PaymentReminder.invoice_id).where(
            PaymentReminder.organization_id == organization_id,
            PaymentReminder.invoice_id.in_(wanted),
            PaymentReminder.sent_on == day,
            PaymentReminder.status.in_(("queued", "sent")),
        )
        return set((await self.session.execute(stmt)).scalars())

    # --- The log -----------------------------------------------------------------------

    async def claim(self, rows: Sequence[dict[str, Any]]) -> set[uuid.UUID]:
        """Insert the reminder rows; an invoice that already has one queued or sent for the day
        is left out (two requests at once cannot both remind). Returns the invoices inserted."""
        if not rows:
            return set()
        stmt = (
            pg_insert(PaymentReminder)
            .values([{"id": new_id(), "status": "queued", **row} for row in rows])
            .on_conflict_do_nothing(
                index_elements=["organization_id", "invoice_id", "sent_on"],
                index_where=text("status IN ('queued', 'sent')"),
            )
            .returning(PaymentReminder.invoice_id)
        )
        return set((await self.session.execute(stmt)).scalars())

    async def batch(self, organization_id: uuid.UUID, batch_id: uuid.UUID) -> list[PaymentReminder]:
        """The reminders that go out in one email, oldest invoice first."""
        stmt = (
            select(PaymentReminder)
            .where(
                PaymentReminder.organization_id == organization_id,
                PaymentReminder.batch_id == batch_id,
            )
            .order_by(PaymentReminder.days_overdue.desc(), PaymentReminder.id)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def history(
        self, organization_id: uuid.UUID, f: ReminderFilters, params: PageParams
    ) -> tuple[list[AnyRow], str | None]:
        """Newest first by ``(created_at, id)``: rows of ``(reminder, client_id, number)``."""
        stmt = (
            select(PaymentReminder, Invoice.client_id.label("client_id"), Invoice.number)
            .select_from(PaymentReminder)
            .join(
                Invoice,
                and_(
                    Invoice.organization_id == PaymentReminder.organization_id,
                    Invoice.id == PaymentReminder.invoice_id,
                ),
            )
            .where(PaymentReminder.organization_id == organization_id)
        )
        if f.client_id is not None:
            stmt = stmt.where(Invoice.client_id == f.client_id)
        if f.invoice_id is not None:
            stmt = stmt.where(PaymentReminder.invoice_id == f.invoice_id)
        if f.status is not None:
            stmt = stmt.where(PaymentReminder.status == f.status)
        if f.source is not None:
            stmt = stmt.where(PaymentReminder.source == f.source)
        if f.date_from is not None:
            stmt = stmt.where(PaymentReminder.sent_on >= f.date_from)
        if f.date_to is not None:
            stmt = stmt.where(PaymentReminder.sent_on <= f.date_to)
        return await keyset_page(
            self.session,
            stmt,
            key=PaymentReminder.created_at,
            id_column=PaymentReminder.id,
            sort="created",
            kind="dt",
            descending=True,
            params=params,
        )


def _overdue(today: date) -> list[Any]:
    """Issued, still owing and past the due date."""
    return [
        Invoice.status == "issued",
        balance_expr() > 0,
        Invoice.due_date.is_not(None),
        Invoice.due_date < today,
    ]
