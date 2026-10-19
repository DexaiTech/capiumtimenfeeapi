"""Aggregates behind the client overview. Every method is scoped to one organization and one
client, and is a single aggregate query (never a query per row)."""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from typing import cast as typing_cast

from sqlalchemy import BigInteger, ColumnElement, cast, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import ColumnExpressionArgument

from app.db.models import (
    AuditLog,
    Client,
    ClientContact,
    Disbursement,
    Invoice,
    Job,
    Payment,
    TimeEntry,
    VatRate,
)
from app.repositories.invoices import DISPLAY_STATUSES, display_status_condition


@dataclass(frozen=True, slots=True)
class TimeFigures:
    minutes_30d: int
    billable_minutes_30d: int
    minutes_ytd: int
    billable_minutes_ytd: int
    last_entry_on: date | None
    entries: int


@dataclass(frozen=True, slots=True)
class DisbursementFigures:
    count: int
    net: int
    gross: int


@dataclass(frozen=True, slots=True)
class InvoiceFigures:
    count: int
    by_status: dict[str, int]
    billed_ytd: int
    last_invoice_on: date | None


@dataclass(frozen=True, slots=True)
class PaymentFigures:
    count: int
    received_ytd: int
    count_ytd: int
    last_payment_on: date | None


def _total(
    expression: ColumnExpressionArgument[Any],
    where: ColumnElement[bool] | None = None,
) -> ColumnElement[int]:
    """``sum`` over the rows that match ``where``, as a BIGINT, 0 over none."""
    summed: ColumnElement[Any] = func.sum(expression)
    if where is not None:
        summed = func.sum(expression).filter(where)
    return cast(func.coalesce(summed, 0), BigInteger)


class ClientOverviewRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def job_counts(self, organization_id: uuid.UUID, client_id: uuid.UUID) -> dict[str, int]:
        """Live jobs of the client by their stored status."""
        stmt = (
            select(Job.status, func.count())
            .where(
                Job.organization_id == organization_id,
                Job.client_id == client_id,
                Job.deleted_at.is_(None),
            )
            .group_by(Job.status)
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all()}

    async def time_figures(
        self,
        organization_id: uuid.UUID,
        client_id: uuid.UUID,
        *,
        last_30d_from: date,
        ytd_from: date,
        today: date,
        user_id: uuid.UUID | None,
    ) -> TimeFigures:
        """Time recorded on the client (``user_id``: only that person's)."""
        entry = TimeEntry
        in_30d = (entry.entry_date >= last_30d_from) & (entry.entry_date <= today)
        in_ytd = (entry.entry_date >= ytd_from) & (entry.entry_date <= today)
        stmt = select(
            func.coalesce(func.sum(entry.minutes).filter(in_30d), 0),
            func.coalesce(func.sum(entry.minutes).filter(in_30d & entry.is_billable), 0),
            func.coalesce(func.sum(entry.minutes).filter(in_ytd), 0),
            func.coalesce(func.sum(entry.minutes).filter(in_ytd & entry.is_billable), 0),
            func.max(entry.entry_date),
            func.count(),
        ).where(entry.organization_id == organization_id, entry.client_id == client_id)
        if user_id is not None:
            stmt = stmt.where(entry.user_id == user_id)
        row = (await self.session.execute(stmt)).one()
        return TimeFigures(
            minutes_30d=int(row[0]),
            billable_minutes_30d=int(row[1]),
            minutes_ytd=int(row[2]),
            billable_minutes_ytd=int(row[3]),
            last_entry_on=row[4],
            entries=int(row[5]),
        )

    async def disbursement_figures(
        self,
        organization_id: uuid.UUID,
        client_id: uuid.UUID,
        *,
        fallback_rate_bp: int,
        created_by: uuid.UUID | None,
    ) -> DisbursementFigures:
        """Count, net and gross of the client's disbursements. VAT per disbursement at its own
        rate, else ``fallback_rate_bp`` (the client's or the practice's default), rounded half
        up like an invoice line."""
        rate = func.coalesce(VatRate.rate_bp, fallback_rate_bp)
        vat = func.div(Disbursement.amount * rate + 5000, 10000)
        stmt = (
            select(func.count(), _total(Disbursement.amount), _total(Disbursement.amount + vat))
            .select_from(Disbursement)
            .outerjoin(
                VatRate,
                (VatRate.organization_id == Disbursement.organization_id)
                & (VatRate.id == Disbursement.vat_rate_id),
            )
            .where(
                Disbursement.organization_id == organization_id,
                Disbursement.client_id == client_id,
            )
        )
        if created_by is not None:
            stmt = stmt.where(Disbursement.created_by == created_by)
        row = (await self.session.execute(stmt)).one()
        return DisbursementFigures(count=int(row[0]), net=int(row[1]), gross=int(row[2]))

    async def invoice_figures(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, *, today: date, ytd_from: date
    ) -> InvoiceFigures:
        """Invoices of the client by display status, the net billed this year and the date of
        the latest issued one."""
        counts = [
            func.count().filter(display_status_condition(status, today))
            for status in DISPLAY_STATUSES
        ]
        stmt = select(
            func.count(),
            *counts,
            _total(
                Invoice.net_total,
                (Invoice.status == "issued")
                & (Invoice.issue_date >= ytd_from)
                & (Invoice.issue_date <= today),
            ),
            func.max(Invoice.issue_date).filter(Invoice.status == "issued"),
        ).where(Invoice.organization_id == organization_id, Invoice.client_id == client_id)
        row = (await self.session.execute(stmt)).one()
        size = len(DISPLAY_STATUSES)
        return InvoiceFigures(
            count=int(row[0]),
            by_status={name: int(row[1 + i]) for i, name in enumerate(DISPLAY_STATUSES)},
            billed_ytd=int(row[1 + size]),
            last_invoice_on=typing_cast("date | None", row[2 + size]),
        )

    async def payment_figures(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, *, today: date, ytd_from: date
    ) -> PaymentFigures:
        """Payments of the client: how many exist, and what was received this year (void
        payments excluded)."""
        live = Payment.voided_at.is_(None)
        in_ytd = live & (Payment.received_on >= ytd_from) & (Payment.received_on <= today)
        stmt = select(
            func.count(),
            _total(Payment.amount, in_ytd),
            func.count().filter(in_ytd),
            func.max(Payment.received_on).filter(live),
        ).where(Payment.organization_id == organization_id, Payment.client_id == client_id)
        row = (await self.session.execute(stmt)).one()
        return PaymentFigures(
            count=int(row[0]),
            received_ytd=int(row[1]),
            count_ytd=int(row[2]),
            last_payment_on=row[3],
        )

    async def contact_count(self, organization_id: uuid.UUID, client_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(
            ClientContact.organization_id == organization_id,
            ClientContact.client_id == client_id,
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def merged_into(
        self, organization_id: uuid.UUID, client_id: uuid.UUID
    ) -> list[uuid.UUID]:
        """Clients that were merged into this one."""
        stmt = select(Client.id).where(
            Client.organization_id == organization_id,
            Client.merged_into_client_id == client_id,
        )
        return list((await self.session.execute(stmt)).scalars())

    async def recent_activity(
        self,
        organization_id: uuid.UUID,
        client_ids: Collection[uuid.UUID],
        prefixes: Sequence[str],
        limit: int,
    ) -> list[AuditLog]:
        """The latest audit entries about the clients, newest first: entries that target a
        client, and entries that name one in their metadata (``client_id``). Only actions that
        start with one of ``prefixes``. Two index-backed lookups, merged."""
        if not prefixes:
            return []
        wanted = [*client_ids]
        allowed = or_(*(AuditLog.action.startswith(p, autoescape=True) for p in prefixes))
        targeting = (
            select(AuditLog)
            .where(
                AuditLog.organization_id == organization_id,
                AuditLog.target_type == "client",
                AuditLog.target_id.in_(wanted),
                allowed,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        )
        naming = (
            select(AuditLog)
            .where(
                AuditLog.organization_id == organization_id,
                # A literal key, so the partial expression index can be used.
                AuditLog.meta.op("->>")(literal_column("'client_id'")).in_(
                    [str(i) for i in wanted]
                ),
                allowed,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        )
        found: dict[uuid.UUID, AuditLog] = {}
        for stmt in (targeting, naming):
            for entry in (await self.session.execute(stmt)).scalars():
                found[entry.id] = entry
        ordered = sorted(found.values(), key=lambda e: (e.created_at, str(e.id)), reverse=True)
        return ordered[:limit]
