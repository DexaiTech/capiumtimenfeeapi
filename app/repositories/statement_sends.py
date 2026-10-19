"""Statement sends (the log of statements emailed) and the clients a statement can go to."""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    DateTime,
    Subquery,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, ClientContact, Invoice, Payment, StatementSend
from app.repositories.expense_paging import AnyRow, SortKind, keyset_page
from app.utils.pagination import PageParams

# A client never sent a statement sorts as if it was sent at the epoch.
_NEVER = datetime(1970, 1, 1, tzinfo=UTC)

SORTS: dict[str, tuple[str, SortKind]] = {
    "client": ("name_key", "str"),
    "balance": ("balance", "int"),
    "overdue": ("overdue", "int"),
    "last_sent": ("last_sent_key", "dt"),
}


@dataclass(frozen=True, slots=True)
class StatementFilters:
    today: date
    client_id: uuid.UUID | None = None
    partner_user_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    min_balance: int | None = None
    overdue_only: bool = False
    never_sent: bool = False
    q: str | None = None


def _sum(expr: Any) -> ColumnElement[int]:
    return cast(func.coalesce(func.sum(expr), 0), BigInteger)


def _filled(column: Any) -> ColumnElement[bool]:
    return and_(column.is_not(None), func.length(func.trim(column)) > 0)


def _matching(organization_id: uuid.UUID, f: StatementFilters) -> Subquery:
    """One row per client that owes something or holds cash on account and matches the filters.
    Columns: ``client_id, client_code, client_name, name_key, partner_user_id, manager_user_id,
    outstanding, on_account, balance, overdue, oldest_overdue_due_date, last_sent_at,
    last_sent_to, last_sent_key, has_recipient``."""
    owing = Invoice.gross_total - Invoice.paid_total - Invoice.credited_total
    past_due = Invoice.due_date < f.today
    owed = (
        select(
            Invoice.client_id.label("client_id"),
            _sum(owing).label("outstanding"),
            _sum(case((past_due, owing), else_=0)).label("overdue"),
            func.min(case((past_due, Invoice.due_date))).label("oldest_overdue_due_date"),
        )
        .where(Invoice.organization_id == organization_id, Invoice.status == "issued", owing > 0)
        .group_by(Invoice.client_id)
        .subquery("owed")
    )
    cash = (
        select(
            Payment.client_id.label("client_id"),
            _sum(Payment.amount - Payment.allocated_total).label("on_account"),
        )
        .where(Payment.organization_id == organization_id, Payment.voided_at.is_(None))
        .group_by(Payment.client_id)
        .subquery("cash")
    )
    last_sends = (
        select(
            StatementSend.client_id.label("client_id"),
            StatementSend.sent_at.label("sent_at"),
            StatementSend.recipients.label("recipients"),
        )
        .where(StatementSend.organization_id == organization_id, StatementSend.status == "sent")
        .distinct(StatementSend.client_id)
        .order_by(StatementSend.client_id, StatementSend.sent_at.desc())
        .subquery("last_sends")
    )
    outstanding = func.coalesce(owed.c.outstanding, 0)
    on_account = func.coalesce(cash.c.on_account, 0)
    overdue = func.coalesce(owed.c.overdue, 0)
    balance = outstanding - on_account
    contact_with_address = exists(
        select(ClientContact.id).where(
            ClientContact.organization_id == Client.organization_id,
            ClientContact.client_id == Client.id,
            or_(ClientContact.is_billing.is_(True), ClientContact.is_primary.is_(True)),
            _filled(ClientContact.email),
        )
    )
    stmt = (
        select(
            Client.id.label("client_id"),
            Client.code.label("client_code"),
            Client.name.label("client_name"),
            func.lower(Client.name).label("name_key"),
            Client.partner_user_id.label("partner_user_id"),
            Client.manager_user_id.label("manager_user_id"),
            outstanding.label("outstanding"),
            on_account.label("on_account"),
            balance.label("balance"),
            overdue.label("overdue"),
            owed.c.oldest_overdue_due_date.label("oldest_overdue_due_date"),
            last_sends.c.sent_at.label("last_sent_at"),
            last_sends.c.recipients.label("last_sent_to"),
            func.coalesce(last_sends.c.sent_at, literal(_NEVER, DateTime(timezone=True))).label(
                "last_sent_key"
            ),
            or_(_filled(Client.invoice_email), contact_with_address, _filled(Client.email)).label(
                "has_recipient"
            ),
        )
        .select_from(Client)
        .outerjoin(owed, owed.c.client_id == Client.id)
        .outerjoin(cash, cash.c.client_id == Client.id)
        .outerjoin(last_sends, last_sends.c.client_id == Client.id)
        .where(
            Client.organization_id == organization_id,
            Client.deleted_at.is_(None),
            or_(outstanding > 0, on_account > 0),
        )
    )
    if f.client_id is not None:
        stmt = stmt.where(Client.id == f.client_id)
    if f.partner_user_id is not None:
        stmt = stmt.where(Client.partner_user_id == f.partner_user_id)
    if f.manager_user_id is not None:
        stmt = stmt.where(Client.manager_user_id == f.manager_user_id)
    if f.min_balance is not None:
        stmt = stmt.where(balance >= f.min_balance)
    if f.overdue_only:
        stmt = stmt.where(overdue > 0)
    if f.never_sent:
        stmt = stmt.where(last_sends.c.sent_at.is_(None))
    if f.q:
        stmt = stmt.where(
            or_(
                Client.name.icontains(f.q, autoescape=True),
                Client.code.icontains(f.q, autoescape=True),
            )
        )
    return stmt.subquery("matching")


class StatementSendRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def page(
        self,
        organization_id: uuid.UUID,
        f: StatementFilters,
        *,
        sort: str,
        scope: str,
        descending: bool,
        params: PageParams,
    ) -> tuple[list[AnyRow], str | None]:
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

    async def totals(self, organization_id: uuid.UUID, f: StatementFilters) -> RowMapping:
        matching = _matching(organization_id, f)
        stmt = select(
            func.count().label("client_count"),
            _sum(matching.c.outstanding).label("outstanding"),
            _sum(matching.c.overdue).label("overdue"),
            _sum(matching.c.on_account).label("on_account"),
            _sum(matching.c.balance).label("balance"),
        ).select_from(matching)
        return (await self.session.execute(stmt)).mappings().one()

    # --- The log -----------------------------------------------------------------------

    async def lock_client(self, organization_id: uuid.UUID, client_id: uuid.UUID) -> None:
        """Serialise the statement requests of one client (held until the transaction ends)."""
        key = f"statement_send:{organization_id}:{client_id}"
        await self.session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
        )

    async def queued_since(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, since: datetime
    ) -> StatementSend | None:
        stmt = (
            select(StatementSend)
            .where(
                StatementSend.organization_id == organization_id,
                StatementSend.client_id == client_id,
                StatementSend.status == "queued",
                StatementSend.created_at >= since,
            )
            .order_by(StatementSend.created_at.desc(), StatementSend.id)
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add(self, send: StatementSend) -> StatementSend:
        self.session.add(send)
        await self.session.flush()
        await self.session.refresh(send)
        return send

    async def get(
        self, organization_id: uuid.UUID, send_id: uuid.UUID, *, for_update: bool = False
    ) -> StatementSend | None:
        stmt = (
            select(StatementSend)
            .where(StatementSend.organization_id == organization_id, StatementSend.id == send_id)
            .execution_options(populate_existing=True)
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()
