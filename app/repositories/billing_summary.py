"""The client's billing summary: what was spent on each job and what it is worth, by source of
value. Every method is scoped to one organization."""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, Integer, and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Disbursement, Job, TimeEntry
from app.repositories.time_entries import wip_time_clause
from app.repositories.wip import eligible_disbursement_conditions

# Rates agreed with the client (a client level of the ladder, or the job's own rate).
CONTRACT_SOURCES = ("client_service_staff", "client_service", "client_staff", "client", "job")


@dataclass(slots=True)
class TimeRollup:
    """The time of one job (``job_id`` None: time on the client without a job)."""

    logged_minutes: int = 0
    billable_minutes: int = 0
    non_billable_minutes: int = 0
    billed_minutes: int = 0
    ready_minutes: int = 0
    awaiting_approval_minutes: int = 0
    company_value: int = 0
    contract_value: int = 0
    ready_value: int = 0
    billed_value: int = 0
    cost_value: int = 0
    ready_cost_value: int = 0


class BillingSummaryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def time_by_job(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, *, require_approval: bool
    ) -> dict[uuid.UUID | None, TimeRollup]:
        """All of the client's time rolled up per job: hours, value by source and cost."""
        entry = TimeEntry
        billable = entry.is_billable.is_(True)
        unbilled = entry.billing_status == "unbilled"
        ready = wip_time_clause(require_approval)
        contract = entry.rate_source.in_(CONTRACT_SOURCES)

        def minutes_where(*conditions: ColumnElement[bool]) -> ColumnElement[int]:
            return func.coalesce(
                func.sum(case((and_(*conditions), entry.minutes), else_=0)), 0
            ).cast(Integer)

        def value_where(column: Any, *conditions: ColumnElement[bool]) -> ColumnElement[Any]:
            return func.coalesce(func.sum(case((and_(*conditions), column), else_=0)), 0)

        stmt = (
            select(
                entry.job_id,
                func.coalesce(func.sum(entry.minutes), 0),
                minutes_where(billable),
                minutes_where(~billable),
                minutes_where(entry.billing_status == "billed"),
                minutes_where(ready),
                minutes_where(billable, unbilled, ~ready),
                value_where(entry.charge_value, billable, unbilled, ~contract),
                value_where(entry.charge_value, billable, unbilled, contract),
                value_where(entry.charge_value, ready),
                value_where(entry.charge_value, billable, entry.billing_status == "billed"),
                func.coalesce(func.sum(entry.cost_value), 0),
                value_where(entry.cost_value, ready),
            )
            .where(entry.organization_id == organization_id, entry.client_id == client_id)
            .group_by(entry.job_id)
        )
        found: dict[uuid.UUID | None, TimeRollup] = {}
        for row in (await self.session.execute(stmt)).all():
            found[row[0]] = TimeRollup(*(int(value) for value in row[1:]))
        return found

    async def ready_time_costs(
        self, organization_id: uuid.UUID, client_id: uuid.UUID, *, require_approval: bool
    ) -> dict[uuid.UUID, int]:
        """The cost of each time entry that is ready to bill (for billing on the team's cost)."""
        stmt = select(TimeEntry.id, TimeEntry.cost_value).where(
            TimeEntry.organization_id == organization_id,
            TimeEntry.client_id == client_id,
            wip_time_clause(require_approval),
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all()}

    async def disbursements_by_job(
        self, organization_id: uuid.UUID, client_id: uuid.UUID
    ) -> dict[uuid.UUID | None, int]:
        """Disbursements ready to bill (the "other costs"), per job."""
        stmt = (
            select(Disbursement.job_id, func.coalesce(func.sum(Disbursement.amount), 0))
            .where(
                *eligible_disbursement_conditions(organization_id),
                Disbursement.client_id == client_id,
            )
            .group_by(Disbursement.job_id)
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all()}

    async def jobs(self, organization_id: uuid.UUID, client_id: uuid.UUID) -> list[Job]:
        """The client's jobs, newest first (every status: the hours spent stay visible)."""
        stmt = (
            select(Job)
            .where(
                Job.organization_id == organization_id,
                Job.client_id == client_id,
                Job.deleted_at.is_(None),
            )
            .order_by(Job.created_at.desc(), Job.id)
        )
        return list((await self.session.execute(stmt)).scalars())
