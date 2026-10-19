"""Debtor queries: what each client owed on issued invoices as at a date.

Balances are *as at* the date, like the Aged Debtors report: an issued invoice dated on or
before it, less the allocations of payments received by then (void and reversed payments do
not count) and the credit notes issued by then. Money is BIGINT pence: ``sum`` is cast back
because PostgreSQL widens it to numeric.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    Subquery,
    Text,
    and_,
    case,
    cast,
    func,
    null,
    select,
)
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, CreditNote, Invoice, Payment, PaymentAllocation, PaymentReminder
from app.repositories.expense_paging import AnyRow, SortKind, keyset_page
from app.utils.pagination import PageParams

BUCKETS = ("current", "days_1_30", "days_31_60", "days_61_90", "days_90_plus")
# What counts as a reminder about an invoice: one that is queued or went out.
LIVE_REMINDERS = ("queued", "sent")
# The most open invoices the drill-down of one client lists.
MAX_CLIENT_INVOICES = 500

# sort name -> (column of the matching subquery, kind of the cursor value)
SORTS: dict[str, tuple[str, SortKind]] = {
    "client": ("name_key", "str"),
    "total": ("total", "int"),
    "current": ("current", "int"),
    "days_1_30": ("days_1_30", "int"),
    "days_31_60": ("days_31_60", "int"),
    "days_61_90": ("days_61_90", "int"),
    "days_90_plus": ("days_90_plus", "int"),
    "oldest_due": ("oldest_due_date", "date"),
}


def _sum(expr: Any) -> ColumnElement[int]:
    return cast(func.coalesce(func.sum(expr), 0), BigInteger)


def _when(condition: Any, value: Any) -> ColumnElement[Any]:
    return case((condition, value), else_=0)


@dataclass(frozen=True, slots=True)
class DebtorFilters:
    as_at: date
    client_id: uuid.UUID | None = None
    partner_user_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    min_balance: int | None = None


def bucket_columns(due: Any, balance: Any, as_at: date) -> list[ColumnElement[int]]:
    """The five bucket sums, labelled, of a balance by its due date as at ``as_at``."""
    d30, d60, d90 = (as_at - timedelta(days=n) for n in (30, 60, 90))
    return [
        _sum(_when(due >= as_at, balance)).label("current"),
        _sum(_when(and_(due < as_at, due >= d30), balance)).label("days_1_30"),
        _sum(_when(and_(due < d30, due >= d60), balance)).label("days_31_60"),
        _sum(_when(and_(due < d60, due >= d90), balance)).label("days_61_90"),
        _sum(_when(due < d90, balance)).label("days_90_plus"),
    ]


def open_invoices(
    organization_id: uuid.UUID, as_at: date, client_id: uuid.UUID | None = None
) -> Subquery:
    """The issued invoices (dated on or before ``as_at``) that still owed something at the end
    of that day. Columns: ``invoice_id, client_id, number, source, issue_date, due_date,
    gross_total, paid, credited, balance`` (``due_date`` falls back to the issue date)."""
    paid_stmt = (
        select(
            PaymentAllocation.invoice_id.label("invoice_id"),
            _sum(PaymentAllocation.amount).label("amount"),
        )
        .join(
            Payment,
            and_(
                Payment.organization_id == PaymentAllocation.organization_id,
                Payment.id == PaymentAllocation.payment_id,
            ),
        )
        .where(
            PaymentAllocation.organization_id == organization_id,
            Payment.received_on <= as_at,
            Payment.voided_at.is_(None),
        )
        .group_by(PaymentAllocation.invoice_id)
    )
    credited_stmt = (
        select(
            CreditNote.invoice_id.label("invoice_id"),
            _sum(CreditNote.gross_total).label("amount"),
        )
        .where(CreditNote.organization_id == organization_id, CreditNote.issue_date <= as_at)
        .group_by(CreditNote.invoice_id)
    )
    if client_id is not None:
        paid_stmt = paid_stmt.where(PaymentAllocation.client_id == client_id)
        credited_stmt = credited_stmt.where(CreditNote.client_id == client_id)
    paid = paid_stmt.subquery("paid")
    credited = credited_stmt.subquery("credited")
    paid_amount = func.coalesce(paid.c.amount, 0)
    credited_amount = func.coalesce(credited.c.amount, 0)
    balance = Invoice.gross_total - paid_amount - credited_amount
    # ``source`` marks opening-balance invoices once the model has the column.
    source_column = getattr(Invoice, "source", None)
    source = source_column if source_column is not None else cast(null(), Text)
    stmt = (
        select(
            Invoice.id.label("invoice_id"),
            Invoice.client_id.label("client_id"),
            Invoice.number.label("number"),
            source.label("source"),
            Invoice.issue_date.label("issue_date"),
            func.coalesce(Invoice.due_date, Invoice.issue_date).label("due_date"),
            Invoice.gross_total.label("gross_total"),
            cast(paid_amount, BigInteger).label("paid"),
            cast(credited_amount, BigInteger).label("credited"),
            cast(balance, BigInteger).label("balance"),
        )
        .select_from(Invoice)
        .outerjoin(paid, paid.c.invoice_id == Invoice.id)
        .outerjoin(credited, credited.c.invoice_id == Invoice.id)
        .where(
            Invoice.organization_id == organization_id,
            Invoice.status == "issued",
            Invoice.issue_date <= as_at,
            balance > 0,
        )
    )
    if client_id is not None:
        stmt = stmt.where(Invoice.client_id == client_id)
    return stmt.subquery("open_invoices")


def _last_reminders(organization_id: uuid.UUID, *, by_client: bool) -> Subquery:
    """Per client (or per invoice): how many reminders were queued or sent, and the latest."""
    moment = func.coalesce(PaymentReminder.sent_at, PaymentReminder.created_at)
    if by_client:
        stmt = (
            select(
                Invoice.client_id.label("client_id"),
                func.count().label("n"),
                func.max(moment).label("last_at"),
            )
            .select_from(PaymentReminder)
            .join(
                Invoice,
                and_(
                    Invoice.organization_id == PaymentReminder.organization_id,
                    Invoice.id == PaymentReminder.invoice_id,
                ),
            )
            .group_by(Invoice.client_id)
        )
    else:
        stmt = select(
            PaymentReminder.invoice_id.label("invoice_id"),
            func.count().label("n"),
            func.max(moment).label("last_at"),
        ).group_by(PaymentReminder.invoice_id)
    stmt = stmt.where(
        PaymentReminder.organization_id == organization_id,
        PaymentReminder.status.in_(LIVE_REMINDERS),
    )
    return stmt.subquery("client_reminders" if by_client else "invoice_reminders")


def _matching(organization_id: uuid.UUID, f: DebtorFilters) -> Subquery:
    """One row per client that owes something and matches the filters: the buckets, the total,
    the number of open invoices, the oldest due date, the client's name, people and the latest
    reminder. Columns: ``client_id, current, days_1_30, ..., total, invoice_count,
    oldest_due_date, client_code, client_name, name_key, partner_user_id, manager_user_id,
    last_reminder_at``."""
    inv = open_invoices(organization_id, f.as_at, f.client_id)
    per_client = (
        select(
            inv.c.client_id.label("client_id"),
            *bucket_columns(inv.c.due_date, inv.c.balance, f.as_at),
            _sum(inv.c.balance).label("total"),
            func.count().label("invoice_count"),
            func.min(inv.c.due_date).label("oldest_due_date"),
        )
        .group_by(inv.c.client_id)
        .subquery("per_client")
    )
    reminders = _last_reminders(organization_id, by_client=True)
    stmt = (
        select(
            per_client,
            Client.code.label("client_code"),
            Client.name.label("client_name"),
            func.lower(Client.name).label("name_key"),
            Client.partner_user_id.label("partner_user_id"),
            Client.manager_user_id.label("manager_user_id"),
            reminders.c.last_at.label("last_reminder_at"),
        )
        .select_from(per_client)
        .join(
            Client,
            and_(Client.organization_id == organization_id, Client.id == per_client.c.client_id),
        )
        .outerjoin(reminders, reminders.c.client_id == per_client.c.client_id)
    )
    if f.partner_user_id is not None:
        stmt = stmt.where(Client.partner_user_id == f.partner_user_id)
    if f.manager_user_id is not None:
        stmt = stmt.where(Client.manager_user_id == f.manager_user_id)
    if f.min_balance is not None:
        stmt = stmt.where(per_client.c.total >= f.min_balance)
    return stmt.subquery("matching")


class DebtorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def page(
        self,
        organization_id: uuid.UUID,
        f: DebtorFilters,
        *,
        sort: str,
        scope: str,
        descending: bool,
        params: PageParams,
    ) -> tuple[list[AnyRow], str | None]:
        """One page of debtor clients by ``sort``; ``scope`` ties a cursor to the filters."""
        matching = _matching(organization_id, f)
        column, kind = SORTS[sort]
        return await keyset_page(
            self.session,
            select(matching),
            key=matching.c[column],
            id_column=matching.c.client_id,
            sort=f"{sort}:{scope}",
            kind=kind,
            descending=descending,
            params=params,
        )

    async def totals(self, organization_id: uuid.UUID, f: DebtorFilters) -> RowMapping:
        """The buckets, total, client and invoice counts of every matching client."""
        matching = _matching(organization_id, f)
        stmt = select(
            *(_sum(matching.c[name]).label(name) for name in (*BUCKETS, "total")),
            func.count().label("client_count"),
            _sum(matching.c.invoice_count).label("invoice_count"),
        ).select_from(matching)
        return (await self.session.execute(stmt)).mappings().one()

    async def client_totals(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, as_at: date
    ) -> RowMapping:
        """The buckets and total of one client's open invoices."""
        inv = open_invoices(organization_id, as_at, client_id)
        stmt = select(
            *bucket_columns(inv.c.due_date, inv.c.balance, as_at),
            _sum(inv.c.balance).label("total"),
        ).select_from(inv)
        return (await self.session.execute(stmt)).mappings().one()

    async def client_invoices(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, as_at: date
    ) -> Sequence[RowMapping]:
        """The client's open invoices, oldest due first, with their reminder counts."""
        inv = open_invoices(organization_id, as_at, client_id)
        reminders = _last_reminders(organization_id, by_client=False)
        stmt = (
            select(
                inv,
                func.coalesce(reminders.c.n, 0).label("reminder_count"),
                reminders.c.last_at.label("last_reminder_at"),
            )
            .select_from(inv)
            .outerjoin(reminders, reminders.c.invoice_id == inv.c.invoice_id)
            .order_by(inv.c.due_date, inv.c.number, inv.c.invoice_id)
            .limit(MAX_CLIENT_INVOICES)
        )
        return (await self.session.execute(stmt)).mappings().all()
