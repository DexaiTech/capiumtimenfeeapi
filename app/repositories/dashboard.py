"""Dashboard queries: a handful of aggregates, each filtered by ``organization_id``."""

import uuid
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy import and_, desc, exists, func, select
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Client,
    Invoice,
    Job,
    JobAssignee,
    OrganizationMember,
    TimeEntry,
    Timer,
    Timesheet,
)
from app.repositories.reports import ItemFilter, invoice_balances, total, when, wip_items

OPEN_JOB_STATUSES = ("planned", "in_progress", "on_hold")


class DashboardRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def week_minutes(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, start: date, end: date
    ) -> RowMapping:
        stmt = select(
            total(TimeEntry.minutes).label("total_minutes"),
            total(when(TimeEntry.is_billable.is_(True), TimeEntry.minutes)).label(
                "chargeable_minutes"
            ),
        ).where(
            TimeEntry.organization_id == organization_id,
            TimeEntry.user_id == user_id,
            TimeEntry.entry_date >= start,
            TimeEntry.entry_date <= end,
        )
        return (await self.session.execute(stmt)).mappings().one()

    async def timer(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> RowMapping | None:
        stmt = (
            select(
                Timer.started_at,
                Timer.accumulated_seconds,
                Timer.client_id,
                Client.name.label("client_name"),
                Timer.job_id,
                Job.number.label("job_number"),
                Job.name.label("job_name"),
                Timer.description,
                Timer.is_billable,
            )
            .select_from(Timer)
            .outerjoin(
                Client,
                and_(Client.organization_id == Timer.organization_id, Client.id == Timer.client_id),
            )
            .outerjoin(
                Job, and_(Job.organization_id == Timer.organization_id, Job.id == Timer.job_id)
            )
            .where(Timer.organization_id == organization_id, Timer.user_id == user_id)
        )
        return (await self.session.execute(stmt)).mappings().one_or_none()

    async def unsubmitted_weeks(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, before: date, limit: int
    ) -> Sequence[RowMapping]:
        """Open or rejected timesheets with time, of weeks before ``before``, newest first.
        Every row carries ``total_count``, the number of such weeks."""
        stmt = (
            select(
                Timesheet.id.label("timesheet_id"),
                Timesheet.week_start,
                Timesheet.status,
                total(TimeEntry.minutes).label("total_minutes"),
                func.count().over().label("total_count"),
            )
            .join(
                TimeEntry,
                and_(
                    TimeEntry.organization_id == Timesheet.organization_id,
                    TimeEntry.timesheet_id == Timesheet.id,
                ),
            )
            .where(
                Timesheet.organization_id == organization_id,
                Timesheet.user_id == user_id,
                Timesheet.status.in_(("open", "rejected")),
                Timesheet.week_start < before,
            )
            .group_by(Timesheet.id, Timesheet.week_start, Timesheet.status)
            .order_by(desc(Timesheet.week_start))
            .limit(limit)
        )
        return (await self.session.execute(stmt)).mappings().all()

    async def jobs_due(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, until: date, limit: int
    ) -> Sequence[RowMapping]:
        assigned = exists().where(
            JobAssignee.organization_id == Job.organization_id,
            JobAssignee.job_id == Job.id,
            JobAssignee.user_id == user_id,
        )
        stmt = (
            select(
                Job.id.label("job_id"),
                Job.number,
                Job.name,
                Job.client_id,
                Client.name.label("client_name"),
                Job.status,
                Job.due_date,
                func.coalesce(Job.manager_user_id == user_id, False).label("is_manager"),
                assigned.label("is_assignee"),
            )
            .join(
                Client,
                and_(Client.organization_id == Job.organization_id, Client.id == Job.client_id),
            )
            .where(
                Job.organization_id == organization_id,
                Job.deleted_at.is_(None),
                Job.status.in_(OPEN_JOB_STATUSES),
                Job.due_date.is_not(None),
                Job.due_date <= until,
                (Job.manager_user_id == user_id) | assigned,
            )
            .order_by(Job.due_date, Job.number)
            .limit(limit)
        )
        return (await self.session.execute(stmt)).mappings().all()

    async def approvals_waiting(
        self, organization_id: uuid.UUID, manager_user_id: uuid.UUID | None
    ) -> int:
        """Submitted timesheets: all of them, or those of the people ``manager_user_id``
        manages."""
        stmt = select(func.count(Timesheet.id)).where(
            Timesheet.organization_id == organization_id, Timesheet.status == "submitted"
        )
        if manager_user_id is not None:
            stmt = stmt.where(
                Timesheet.user_id.in_(
                    select(OrganizationMember.user_id).where(
                        OrganizationMember.organization_id == organization_id,
                        OrganizationMember.manager_user_id == manager_user_id,
                    )
                )
            )
        return int((await self.session.scalar(stmt)) or 0)

    async def wip_totals(
        self, organization_id: uuid.UUID, as_at: date, require_approval: bool
    ) -> RowMapping:
        items = wip_items(organization_id, as_at, require_approval, ItemFilter())
        value = items.c.time_value + items.c.disbursement_value
        stmt = select(
            total(value).label("total"),
            total(when(items.c.item_date < as_at - timedelta(days=90), value)).label("over_90"),
        ).select_from(items)
        return (await self.session.execute(stmt)).mappings().one()

    async def top_wip_clients(
        self, organization_id: uuid.UUID, as_at: date, require_approval: bool, limit: int
    ) -> Sequence[RowMapping]:
        items = wip_items(organization_id, as_at, require_approval, ItemFilter())
        amount = total(items.c.time_value + items.c.disbursement_value)
        stmt = (
            select(
                Client.id.label("client_id"),
                Client.code.label("client_code"),
                Client.name.label("client_name"),
                amount.label("amount"),
                func.min(items.c.item_date).label("oldest_date"),
            )
            .select_from(items)
            .join(
                Client,
                and_(Client.organization_id == organization_id, Client.id == items.c.client_id),
            )
            .group_by(Client.id, Client.code, Client.name)
            .order_by(desc("amount"), Client.name, Client.id)
            .limit(limit)
        )
        rows: Sequence[RowMapping] = (await self.session.execute(stmt)).mappings().all()
        return rows

    async def debtor_totals(self, organization_id: uuid.UUID, as_at: date) -> RowMapping:
        balances = invoice_balances(organization_id, as_at, ItemFilter())
        stmt = select(
            total(balances.c.balance).label("total"),
            total(when(balances.c.due_date < as_at, balances.c.balance)).label("overdue"),
        ).where(balances.c.balance > 0)
        return (await self.session.execute(stmt)).mappings().one()

    async def top_overdue_clients(
        self, organization_id: uuid.UUID, as_at: date, limit: int
    ) -> Sequence[RowMapping]:
        balances = invoice_balances(organization_id, as_at, ItemFilter())
        amount = total(balances.c.balance)
        stmt = (
            select(
                Client.id.label("client_id"),
                Client.code.label("client_code"),
                Client.name.label("client_name"),
                amount.label("amount"),
                func.min(balances.c.due_date).label("oldest_date"),
            )
            .select_from(balances)
            .join(
                Client,
                and_(Client.organization_id == organization_id, Client.id == balances.c.client_id),
            )
            .where(balances.c.balance > 0, balances.c.due_date < as_at)
            .group_by(Client.id, Client.code, Client.name)
            .order_by(desc("amount"), Client.name, Client.id)
            .limit(limit)
        )
        rows: Sequence[RowMapping] = (await self.session.execute(stmt)).mappings().all()
        return rows

    async def billed_net(
        self, organization_id: uuid.UUID, periods: Sequence[tuple[date, date]]
    ) -> list[int]:
        """Net of issued invoices per inclusive date range, in one query."""
        columns: list[Any] = [
            total(
                when(
                    and_(Invoice.issue_date >= start, Invoice.issue_date <= end),
                    Invoice.net_total,
                )
            ).label(f"p{i}")
            for i, (start, end) in enumerate(periods)
        ]
        stmt = select(*columns).where(
            Invoice.organization_id == organization_id,
            Invoice.status == "issued",
            Invoice.issue_date >= min(start for start, _ in periods),
            Invoice.issue_date <= max(end for _, end in periods),
        )
        row = (await self.session.execute(stmt)).one()
        return [int(value or 0) for value in row]
