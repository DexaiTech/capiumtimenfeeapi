"""Debtors: who owes what on issued invoices, aged by due date, as at a date.

The figures are the ones of the Aged Debtors report (``repositories.debtors`` repeats its
as-at rule); this module adds the per-client paging, filters and the invoice drill-down.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.repositories.billing_common import BillingReferenceRepository
from app.repositories.debtors import DebtorFilters, DebtorRepository
from app.schemas.billing import ClientRef
from app.schemas.debtors import (
    Bucket,
    DebtorBucketTotals,
    DebtorClientInvoicesRead,
    DebtorInvoiceRead,
    DebtorRow,
    DebtorSort,
    DebtorsRead,
    DebtorTotals,
    OpenStatus,
    SortOrder,
)
from app.services import policy
from app.services.billing_common import get_client_or_404, today_for
from app.services.policy import OrgContext
from app.utils.pagination import PageParams

# Any of these lets a person see who owes what.
READ_CODES = ("payment.read", "invoice.read", "report.financial")


@dataclass(frozen=True, slots=True)
class DebtorQuery:
    as_at: date | None = None
    client_id: uuid.UUID | None = None
    partner_user_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    min_balance: int | None = None
    sort: DebtorSort = "client"
    order: SortOrder | None = None


def bucket_of(days_overdue: int) -> Bucket:
    """The bucket of an invoice that is ``days_overdue`` days past its due date (0: not due)."""
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "days_1_30"
    if days_overdue <= 60:
        return "days_31_60"
    if days_overdue <= 90:
        return "days_61_90"
    return "days_90_plus"


def _scope(f: DebtorFilters) -> str:
    """Identifies the filters a cursor was issued for."""
    raw = "|".join(
        str(v) for v in (f.as_at, f.client_id, f.partner_user_id, f.manager_user_id, f.min_balance)
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:10]


class DebtorService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.debtors = DebtorRepository(session)
        self.refs = BillingReferenceRepository(session)

    async def list_debtors(
        self, ctx: OrgContext, query: DebtorQuery, params: PageParams
    ) -> tuple[DebtorsRead, str | None]:
        policy.ensure_any(ctx, *READ_CODES)
        as_at = self._as_at(ctx, query.as_at)
        filters = DebtorFilters(
            as_at=as_at,
            client_id=query.client_id,
            partner_user_id=query.partner_user_id,
            manager_user_id=query.manager_user_id,
            min_balance=query.min_balance,
        )
        descending = (query.order or ("asc" if query.sort == "client" else "desc")) == "desc"
        rows, next_cursor = await self.debtors.page(
            ctx.organization_id,
            filters,
            sort=query.sort,
            scope=_scope(filters),
            descending=descending,
            params=params,
        )
        totals = await self.debtors.totals(ctx.organization_id, filters)
        data = DebtorsRead(
            as_at=as_at,
            currency=ctx.organization.currency,
            totals=DebtorTotals(
                current=totals["current"],
                days_1_30=totals["days_1_30"],
                days_31_60=totals["days_31_60"],
                days_61_90=totals["days_61_90"],
                days_90_plus=totals["days_90_plus"],
                total=totals["total"],
                overdue=totals["total"] - totals["current"],
                client_count=totals["client_count"],
                invoice_count=totals["invoice_count"],
            ),
            rows=[
                DebtorRow(
                    client=ClientRef(id=r.client_id, code=r.client_code, name=r.client_name),
                    partner_user_id=r.partner_user_id,
                    manager_user_id=r.manager_user_id,
                    current=r.current,
                    days_1_30=r.days_1_30,
                    days_31_60=r.days_31_60,
                    days_61_90=r.days_61_90,
                    days_90_plus=r.days_90_plus,
                    total=r.total,
                    overdue=r.total - r.current,
                    invoice_count=r.invoice_count,
                    oldest_due_date=r.oldest_due_date,
                    max_days_overdue=(
                        max(0, (as_at - r.oldest_due_date).days) if r.oldest_due_date else 0
                    ),
                    last_reminder_at=r.last_reminder_at,
                )
                for r in rows
            ],
        )
        return data, next_cursor

    async def client_invoices(
        self, ctx: OrgContext, client_id: uuid.UUID, as_at: date | None
    ) -> DebtorClientInvoicesRead:
        policy.ensure_any(ctx, *READ_CODES)
        client = await get_client_or_404(self.refs, ctx.organization_id, client_id)
        day = self._as_at(ctx, as_at)
        totals = await self.debtors.client_totals(ctx.organization_id, client.id, day)
        invoices: list[DebtorInvoiceRead] = []
        for row in await self.debtors.client_invoices(ctx.organization_id, client.id, day):
            days = max(0, (day - row["due_date"]).days)
            status: OpenStatus = (
                "overdue" if days > 0 else ("part_paid" if row["paid"] else "issued")
            )
            invoices.append(
                DebtorInvoiceRead(
                    id=row["invoice_id"],
                    number=row["number"],
                    source=row["source"],
                    issue_date=row["issue_date"],
                    due_date=row["due_date"],
                    days_overdue=days,
                    bucket=bucket_of(days),
                    gross_total=row["gross_total"],
                    paid=row["paid"],
                    credited=row["credited"],
                    outstanding=row["balance"],
                    display_status=status,
                    last_reminder_at=row["last_reminder_at"],
                    reminder_count=row["reminder_count"],
                )
            )
        return DebtorClientInvoicesRead(
            client=ClientRef.from_model(client),
            as_at=day,
            totals=DebtorBucketTotals(
                current=totals["current"],
                days_1_30=totals["days_1_30"],
                days_31_60=totals["days_31_60"],
                days_61_90=totals["days_61_90"],
                days_90_plus=totals["days_90_plus"],
                total=totals["total"],
            ),
            invoices=invoices,
        )

    @staticmethod
    def _as_at(ctx: OrgContext, requested: date | None) -> date:
        today = today_for(ctx.organization)
        if requested is None:
            return today
        if requested > today:
            raise ValidationError.for_field("as_at", "as_at cannot be in the future.")
        return requested
