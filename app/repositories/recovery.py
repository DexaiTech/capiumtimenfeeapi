"""Recovery analysis queries (docs/modules/reports.md, "Recovery analysis").

The billable time of a scope in "pieces": one row per job, client, person, day, overtime flag and
billing status, with the minutes, the value at the rates the time was priced at, its cost and what
the invoices it is on (issued or draft) bill it at. Every query is scoped to one organization;
money sums are cast back to ``BIGINT`` so every figure arrives as a Python ``int``.
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, Job, OrganizationMember, TimeEntry, User, WipAllocation
from app.repositories.reports import DateRange, ItemFilter, item_conditions, total


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


@dataclass(frozen=True, slots=True)
class ClientName:
    id: uuid.UUID
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class PersonName:
    user_id: uuid.UUID
    full_name: str
    initials: str | None


class RecoveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def time_pieces(
        self, organization_id: uuid.UUID, period: DateRange, f: ItemFilter
    ) -> Sequence[RowMapping]:
        """The billable time in ``period`` that matches ``f``, grouped by ``job_id, client_id,
        user_id, entry_date, is_overtime, billing_status``: ``minutes``, ``charge_value``,
        ``cost_value`` and ``billed_value`` (the live allocation of the time to an invoice, issued
        or draft; 0 for time that is unbilled or written off)."""
        billed = (
            select(
                WipAllocation.time_entry_id.label("time_entry_id"),
                WipAllocation.billed_value.label("billed_value"),
            )
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
            TimeEntry.billing_status,
        )
        stmt = (
            select(
                *keys,
                total(TimeEntry.minutes).label("minutes"),
                total(TimeEntry.charge_value).label("charge_value"),
                total(TimeEntry.cost_value).label("cost_value"),
                total(billed.c.billed_value).label("billed_value"),
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
                TimeEntry.is_billable.is_(True),
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

    async def unbilled_pieces(
        self, organization_id: uuid.UUID, job_ids: Collection[uuid.UUID]
    ) -> Sequence[RowMapping]:
        """All the unbilled billable time of these jobs, whatever its date or person, grouped as
        ``time_pieces`` groups it: the weights a fixed fee still to bill is spread by."""
        if not job_ids:
            return []
        keys = (TimeEntry.job_id, TimeEntry.user_id, TimeEntry.entry_date, TimeEntry.is_overtime)
        stmt = (
            select(
                *keys,
                total(TimeEntry.minutes).label("minutes"),
                total(TimeEntry.charge_value).label("charge_value"),
            )
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.job_id.in_(list(job_ids)),
                TimeEntry.is_billable.is_(True),
                TimeEntry.billing_status == "unbilled",
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
        stmt = select(
            Job.id,
            Job.client_id,
            Job.number,
            Job.name,
            Job.status,
            Job.billing_basis,
            Job.fixed_fee,
            Job.hourly_rate,
        ).where(Job.organization_id == organization_id, Job.id.in_(list(job_ids)))
        return {row[0]: JobTerms(*row) for row in (await self.session.execute(stmt)).all()}

    async def client_jobs(self, organization_id: uuid.UUID, client_id: uuid.UUID) -> list[JobTerms]:
        """The client's live jobs, newest first."""
        stmt = (
            select(
                Job.id,
                Job.client_id,
                Job.number,
                Job.name,
                Job.status,
                Job.billing_basis,
                Job.fixed_fee,
                Job.hourly_rate,
            )
            .where(
                Job.organization_id == organization_id,
                Job.client_id == client_id,
                Job.deleted_at.is_(None),
            )
            .order_by(Job.created_at.desc(), Job.id)
        )
        return [JobTerms(*row) for row in (await self.session.execute(stmt)).all()]

    async def clients(
        self, organization_id: uuid.UUID, client_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, ClientName]:
        if not client_ids:
            return {}
        stmt = select(Client.id, Client.code, Client.name).where(
            Client.organization_id == organization_id, Client.id.in_(list(client_ids))
        )
        return {row[0]: ClientName(*row) for row in (await self.session.execute(stmt)).all()}

    async def people(
        self, organization_id: uuid.UUID, user_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, PersonName]:
        if not user_ids:
            return {}
        stmt = (
            select(OrganizationMember.user_id, User.full_name, OrganizationMember.initials)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id.in_(list(user_ids)),
            )
        )
        return {row[0]: PersonName(*row) for row in (await self.session.execute(stmt)).all()}
