"""Recovery analysis queries (docs/modules/reports.md, "Recovery analysis").

The time of a scope in "pieces": one row per job, client, person, day, overtime flag, billable
flag and billing status, with the minutes, the value at the rates the time was priced at, its
cost, what the invoices it is on bill it at and the share of the credit notes allocated to those
invoices. Every query is scoped to one organization; money sums are cast back to ``BIGINT`` so
every figure arrives as a Python ``int`` (the credited share is a ``numeric`` sum, rounded by the
caller).
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from sqlalchemy import Numeric, and_, cast, exists, func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Client,
    CreditNote,
    CreditNoteWip,
    Invoice,
    Job,
    OrganizationMember,
    Payment,
    TimeEntry,
    User,
    WipAllocation,
)
from app.repositories.reports import DateRange, ItemFilter, item_conditions, total
from app.repositories.staff import person_name


@dataclass(frozen=True, slots=True)
class JobTerms:
    """What the analysis needs to know about a job."""

    id: uuid.UUID
    client_id: uuid.UUID
    number: str
    name: str
    status: str
    billing_basis: str
    fixed_fee: int | None
    hourly_rate: int | None
    cost_plus_markup_bp: int | None


@dataclass(frozen=True, slots=True)
class ClientName:
    id: uuid.UUID
    code: str
    name: str
    cost_plus_markup_bp: int | None


@dataclass(frozen=True, slots=True)
class PersonName:
    user_id: uuid.UUID
    full_name: str
    initials: str | None


_JOB_COLUMNS = (
    Job.id,
    Job.client_id,
    Job.number,
    Job.name,
    Job.status,
    Job.billing_basis,
    Job.fixed_fee,
    Job.hourly_rate,
    Job.cost_plus_markup_bp,
)


class RecoveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def time_pieces(
        self,
        organization_id: uuid.UUID,
        period: DateRange,
        f: ItemFilter,
        *,
        billable_only: bool = True,
    ) -> Sequence[RowMapping]:
        """The time in ``period`` that matches ``f`` (billable time only unless asked), grouped by
        ``job_id, client_id, user_id, entry_date, is_overtime, is_billable, billing_status``:
        ``minutes``, ``charge_value``, ``cost_value``, ``billed_value`` (the live allocation of the
        time to an invoice, issued or draft; 0 for time that is unbilled or written off) and
        ``credited`` (the time's share, by value, of the net of the credit notes allocated to its
        invoice; a credit note whose work was returned to WIP is left out, since that work has
        left the invoice instead)."""
        returned = exists().where(
            CreditNoteWip.organization_id == CreditNote.organization_id,
            CreditNoteWip.credit_note_id == CreditNote.id,
            CreditNoteWip.mode == "return",
        )
        credits = (
            select(
                CreditNote.invoice_id.label("invoice_id"),
                func.sum(CreditNote.net_total).label("net"),
            )
            .where(CreditNote.organization_id == organization_id, ~returned)
            .group_by(CreditNote.invoice_id)
            .subquery("credits")
        )
        share = (
            cast(WipAllocation.billed_value, Numeric)
            * func.coalesce(credits.c.net, 0)
            / func.nullif(Invoice.net_total, 0)
        )
        billed = (
            select(
                WipAllocation.time_entry_id.label("time_entry_id"),
                WipAllocation.billed_value.label("billed_value"),
                func.coalesce(share, 0).label("credited"),
            )
            .join(
                Invoice,
                and_(
                    Invoice.organization_id == WipAllocation.organization_id,
                    Invoice.id == WipAllocation.invoice_id,
                ),
            )
            .outerjoin(credits, credits.c.invoice_id == Invoice.id)
            .where(
                WipAllocation.organization_id == organization_id,
                WipAllocation.time_entry_id.is_not(None),
                WipAllocation.invoice_id.is_not(None),
                WipAllocation.reversed_at.is_(None),
            )
            .subquery("billed")
        )
        keys = (
            TimeEntry.job_id,
            TimeEntry.client_id,
            TimeEntry.user_id,
            TimeEntry.entry_date,
            TimeEntry.is_overtime,
            TimeEntry.is_billable,
            TimeEntry.billing_status,
        )
        stmt = (
            select(
                *keys,
                total(TimeEntry.minutes).label("minutes"),
                total(TimeEntry.charge_value).label("charge_value"),
                total(TimeEntry.cost_value).label("cost_value"),
                total(billed.c.billed_value).label("billed_value"),
                func.coalesce(func.sum(billed.c.credited), 0).label("credited"),
            )
            .select_from(TimeEntry)
            .outerjoin(
                Job,
                and_(Job.organization_id == TimeEntry.organization_id, Job.id == TimeEntry.job_id),
            )
            .outerjoin(
                Client,
                and_(
                    Client.organization_id == TimeEntry.organization_id,
                    Client.id == TimeEntry.client_id,
                ),
            )
            .outerjoin(billed, billed.c.time_entry_id == TimeEntry.id)
            .where(
                TimeEntry.organization_id == organization_id,
                *([TimeEntry.is_billable.is_(True)] if billable_only else []),
                *period.contains(TimeEntry.entry_date),
                *item_conditions(
                    f,
                    client_col=TimeEntry.client_id,
                    job_col=TimeEntry.job_id,
                    user_col=TimeEntry.user_id,
                    activity_col=TimeEntry.activity_code_id,
                ),
            )
            .group_by(*keys)
            .order_by(*keys)
        )
        rows: Sequence[RowMapping] = (await self.session.execute(stmt)).mappings().all()
        return rows

    async def jobs(
        self, organization_id: uuid.UUID, job_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, JobTerms]:
        """The terms of these jobs (deleted jobs included: their time remains)."""
        if not job_ids:
            return {}
        stmt = select(*_JOB_COLUMNS).where(
            Job.organization_id == organization_id, Job.id.in_(list(job_ids))
        )
        return {row[0]: JobTerms(*row) for row in (await self.session.execute(stmt)).all()}

    async def clients(
        self, organization_id: uuid.UUID, client_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, ClientName]:
        if not client_ids:
            return {}
        stmt = select(Client.id, Client.code, Client.name, Client.cost_plus_markup_bp).where(
            Client.organization_id == organization_id, Client.id.in_(list(client_ids))
        )
        return {row[0]: ClientName(*row) for row in (await self.session.execute(stmt)).all()}

    async def people(
        self, organization_id: uuid.UUID, user_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, PersonName]:
        if not user_ids:
            return {}
        stmt = (
            select(OrganizationMember.user_id, person_name(), OrganizationMember.initials)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id.in_(list(user_ids)),
            )
        )
        return {row[0]: PersonName(*row) for row in (await self.session.execute(stmt)).all()}

    async def credit_on_account(self, organization_id: uuid.UUID, client_id: uuid.UUID) -> int:
        """Money the client paid that is not allocated to any invoice (live payments): a credit
        balance on the account, for a future invoice."""
        stmt = select(total(Payment.amount - Payment.allocated_total)).where(
            Payment.organization_id == organization_id,
            Payment.client_id == client_id,
            Payment.voided_at.is_(None),
            Payment.reversed_at.is_(None),
        )
        return int((await self.session.execute(stmt)).scalar_one())
