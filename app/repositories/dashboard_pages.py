"""Queries of the dashboard pages; every one is filtered by ``organization_id``.

The headline figures come from :mod:`app.repositories.dashboard` and the report helpers of
:mod:`app.repositories.reports`; this module adds the queues, the actions, the lists and the
breakdowns the pages need on top of them.
"""

import uuid
from collections.abc import Collection, Sequence
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import (
    BigInteger,
    Select,
    and_,
    case,
    cast,
    desc,
    distinct,
    exists,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Client,
    ExpenseClaim,
    Invoice,
    Job,
    JobAssignee,
    JobBudgetAlert,
    Task,
    TaskAssignee,
    TimeEntry,
    Timesheet,
    User,
)
from app.repositories.dashboard import OPEN_JOB_STATUSES
from app.repositories.expense_claims import claim_gross
from app.repositories.reports import (
    ItemFilter,
    debt_buckets,
    invoice_balances,
    total,
    when,
    wip_items,
)

type AnySelect = Select[*tuple[Any, ...]]


def _in_scope(stmt: AnySelect, column: Any, user_ids: Collection[uuid.UUID] | None) -> AnySelect:
    return stmt if user_ids is None else stmt.where(column.in_(list(user_ids)))


def _not_own(stmt: AnySelect, column: Any, exclude_user_id: uuid.UUID | None) -> AnySelect:
    return stmt if exclude_user_id is None else stmt.where(column != exclude_user_id)


class DashboardPagesRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _rows(self, stmt: AnySelect) -> Sequence[RowMapping]:
        rows: Sequence[RowMapping] = (await self.session.execute(stmt)).mappings().all()
        return rows

    # --- Approval queues ------------------------------------------------------------------

    async def timesheet_queue(
        self,
        organization_id: uuid.UUID,
        *,
        user_ids: Collection[uuid.UUID] | None,
        exclude_user_id: uuid.UUID | None,
    ) -> tuple[int, date | None]:
        """Submitted timesheets of these people (``None``: everyone): how many, and the oldest
        week."""
        stmt: AnySelect = select(func.count(Timesheet.id), func.min(Timesheet.week_start)).where(
            Timesheet.organization_id == organization_id, Timesheet.status == "submitted"
        )
        stmt = _not_own(
            _in_scope(stmt, Timesheet.user_id, user_ids), Timesheet.user_id, exclude_user_id
        )
        row = (await self.session.execute(stmt)).one()
        return int(row[0]), row[1]

    async def waiting_timesheets(
        self,
        organization_id: uuid.UUID,
        *,
        user_ids: Collection[uuid.UUID] | None,
        exclude_user_id: uuid.UUID | None,
        limit: int,
    ) -> Sequence[RowMapping]:
        """The oldest submitted timesheets, with the person's name."""
        stmt: AnySelect = (
            select(
                Timesheet.id,
                Timesheet.user_id,
                User.full_name.label("person"),
                Timesheet.week_start,
                Timesheet.submitted_at,
            )
            .join(User, User.id == Timesheet.user_id)
            .where(Timesheet.organization_id == organization_id, Timesheet.status == "submitted")
        )
        stmt = _not_own(
            _in_scope(stmt, Timesheet.user_id, user_ids), Timesheet.user_id, exclude_user_id
        )
        return await self._rows(stmt.order_by(Timesheet.week_start, Timesheet.id).limit(limit))

    async def expense_queue(
        self,
        organization_id: uuid.UUID,
        *,
        user_ids: Collection[uuid.UUID] | None,
        exclude_user_id: uuid.UUID | None,
    ) -> tuple[int, datetime | None]:
        """Submitted expense claims of these people: how many, and the oldest submission."""
        stmt: AnySelect = select(
            func.count(ExpenseClaim.id), func.min(ExpenseClaim.submitted_at)
        ).where(ExpenseClaim.organization_id == organization_id, ExpenseClaim.status == "submitted")
        stmt = _not_own(
            _in_scope(stmt, ExpenseClaim.user_id, user_ids), ExpenseClaim.user_id, exclude_user_id
        )
        row = (await self.session.execute(stmt)).one()
        return int(row[0]), row[1]

    async def waiting_expense_claims(
        self,
        organization_id: uuid.UUID,
        *,
        user_ids: Collection[uuid.UUID] | None,
        exclude_user_id: uuid.UUID | None,
        limit: int,
    ) -> Sequence[RowMapping]:
        """The oldest submitted expense claims with the claimant's name and the gross total."""
        stmt: AnySelect = (
            select(
                ExpenseClaim.id,
                ExpenseClaim.user_id,
                User.full_name.label("person"),
                ExpenseClaim.title,
                ExpenseClaim.submitted_at,
                claim_gross().label("gross"),
            )
            .join(User, User.id == ExpenseClaim.user_id)
            .where(
                ExpenseClaim.organization_id == organization_id, ExpenseClaim.status == "submitted"
            )
        )
        stmt = _not_own(
            _in_scope(stmt, ExpenseClaim.user_id, user_ids), ExpenseClaim.user_id, exclude_user_id
        )
        return await self._rows(
            stmt.order_by(ExpenseClaim.submitted_at, ExpenseClaim.id).limit(limit)
        )

    # --- My actions -----------------------------------------------------------------------

    async def timesheet_backlog(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, before: date
    ) -> Sequence[RowMapping]:
        """Per status (``open``, ``rejected``): the person's timesheets with time of weeks
        before ``before``: how many and the oldest week."""
        stmt = (
            select(
                Timesheet.status,
                func.count(distinct(Timesheet.id)).label("count"),
                func.min(Timesheet.week_start).label("oldest_week"),
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
                Timesheet.approval_exempt.is_(False),
                Timesheet.week_start < before,
            )
            .group_by(Timesheet.status)
        )
        return await self._rows(stmt)

    async def returned_claims(
        self, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[int, datetime | None]:
        """The person's expense claims returned to them: how many, and the oldest return."""
        stmt = select(func.count(ExpenseClaim.id), func.min(ExpenseClaim.decided_at)).where(
            ExpenseClaim.organization_id == organization_id,
            ExpenseClaim.user_id == user_id,
            ExpenseClaim.status == "returned",
        )
        row = (await self.session.execute(stmt)).one()
        return int(row[0]), row[1]

    async def draft_invoices(
        self, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[int, int, datetime | None]:
        """Draft invoices the person prepared: how many, their gross total, the oldest."""
        stmt = select(
            func.count(Invoice.id),
            total(Invoice.gross_total),
            func.min(Invoice.created_at),
        ).where(
            Invoice.organization_id == organization_id,
            Invoice.created_by == user_id,
            Invoice.status == "draft",
        )
        row = (await self.session.execute(stmt)).one()
        return int(row[0]), int(row[1]), row[2]

    async def overdue_tasks(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, today: date, limit: int
    ) -> Sequence[RowMapping]:
        """Open tasks assigned to the person that are past their due date, oldest first."""
        stmt = (
            select(
                Task.id.label("task_id"),
                Task.title,
                Task.due_date,
                Job.id.label("job_id"),
                Job.number.label("job_number"),
                Job.name.label("job_name"),
                Client.name.label("client_name"),
            )
            .join(
                TaskAssignee,
                and_(
                    TaskAssignee.organization_id == Task.organization_id,
                    TaskAssignee.task_id == Task.id,
                    TaskAssignee.user_id == user_id,
                ),
            )
            .join(Job, and_(Job.organization_id == Task.organization_id, Job.id == Task.job_id))
            .join(
                Client,
                and_(Client.organization_id == Job.organization_id, Client.id == Job.client_id),
            )
            .where(
                Task.organization_id == organization_id,
                Task.status != "completed",
                Task.due_date < today,
                Job.deleted_at.is_(None),
                Job.status.in_(OPEN_JOB_STATUSES),
            )
            .order_by(Task.due_date, Task.id)
            .limit(limit)
        )
        return await self._rows(stmt)

    async def budget_alerts(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, limit: int
    ) -> Sequence[RowMapping]:
        """Open jobs the person manages or is assigned to that are above a budget threshold:
        the highest threshold and usage, worst first."""
        assigned = exists().where(
            JobAssignee.organization_id == Job.organization_id,
            JobAssignee.job_id == Job.id,
            JobAssignee.user_id == user_id,
        )
        stmt = (
            select(
                Job.id.label("job_id"),
                Job.number.label("job_number"),
                Job.name.label("job_name"),
                Client.name.label("client_name"),
                func.max(JobBudgetAlert.threshold).label("threshold"),
                func.max(JobBudgetAlert.usage_bp).label("usage_bp"),
            )
            .select_from(JobBudgetAlert)
            .join(
                Job,
                and_(
                    Job.organization_id == JobBudgetAlert.organization_id,
                    Job.id == JobBudgetAlert.job_id,
                ),
            )
            .join(
                Client,
                and_(Client.organization_id == Job.organization_id, Client.id == Job.client_id),
            )
            .where(
                JobBudgetAlert.organization_id == organization_id,
                Job.deleted_at.is_(None),
                Job.status.in_(OPEN_JOB_STATUSES),
                or_(Job.manager_user_id == user_id, assigned),
            )
            .group_by(Job.id, Job.number, Job.name, Client.name)
            .order_by(desc("threshold"), desc("usage_bp"), Job.number)
            .limit(limit)
        )
        return await self._rows(stmt)

    # --- My timesheet ---------------------------------------------------------------------

    async def week_entries(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, start: date, end: date, limit: int
    ) -> Sequence[RowMapping]:
        """The person's latest entries of the days, newest first. Every row carries
        ``total_count``, the number of entries in the range."""
        stmt = (
            select(
                TimeEntry.id,
                TimeEntry.entry_date,
                TimeEntry.minutes,
                TimeEntry.client_id,
                Client.name.label("client_name"),
                TimeEntry.job_id,
                Job.number.label("job_number"),
                Job.name.label("job_name"),
                TimeEntry.description,
                TimeEntry.is_billable,
                TimeEntry.billing_status,
                func.count().over().label("total_count"),
            )
            .select_from(TimeEntry)
            .outerjoin(
                Client,
                and_(
                    Client.organization_id == TimeEntry.organization_id,
                    Client.id == TimeEntry.client_id,
                ),
            )
            .outerjoin(
                Job,
                and_(Job.organization_id == TimeEntry.organization_id, Job.id == TimeEntry.job_id),
            )
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.user_id == user_id,
                TimeEntry.entry_date >= start,
                TimeEntry.entry_date <= end,
            )
            .order_by(desc(TimeEntry.entry_date), desc(TimeEntry.created_at), desc(TimeEntry.id))
            .limit(limit)
        )
        return await self._rows(stmt)

    # --- Missing timesheets ---------------------------------------------------------------

    async def sheet_statuses(
        self,
        organization_id: uuid.UUID,
        user_ids: Collection[uuid.UUID],
        weeks: Collection[date],
    ) -> dict[tuple[uuid.UUID, date], str]:
        """The status of each person's timesheet of each of these weeks that has one."""
        if not user_ids or not weeks:
            return {}
        stmt = select(Timesheet.user_id, Timesheet.week_start, Timesheet.status).where(
            Timesheet.organization_id == organization_id,
            Timesheet.user_id.in_(list(user_ids)),
            Timesheet.week_start.in_(list(weeks)),
        )
        return {(row[0], row[1]): row[2] for row in (await self.session.execute(stmt)).all()}

    # --- WIP by client --------------------------------------------------------------------

    async def wip_clients(
        self, organization_id: uuid.UUID, as_at: date, require_approval: bool, limit: int
    ) -> Sequence[RowMapping]:
        """The clients with the most WIP as at ``as_at`` (largest first). Every row carries
        ``all_amount`` (the practice's WIP) and ``client_count`` (clients with WIP)."""
        items = wip_items(organization_id, as_at, require_approval, ItemFilter())
        value = items.c.time_value + items.c.disbursement_value
        amount = total(value)
        stmt = (
            select(
                Client.id.label("client_id"),
                Client.code.label("client_code"),
                Client.name.label("client_name"),
                total(items.c.time_value).label("time_value"),
                total(items.c.disbursement_value).label("disbursement_value"),
                total(items.c.minutes).label("minutes"),
                amount.label("amount"),
                func.min(items.c.item_date).label("oldest_date"),
                total(when(items.c.item_date < as_at - timedelta(days=90), value)).label("over_90"),
                cast(func.sum(amount).over(), BigInteger).label("all_amount"),
                func.count().over().label("client_count"),
            )
            .select_from(items)
            .join(
                Client,
                and_(Client.organization_id == organization_id, Client.id == items.c.client_id),
            )
            .group_by(Client.id, Client.code, Client.name)
            .order_by(desc("amount"), func.lower(Client.name), Client.id)
            .limit(limit)
        )
        return await self._rows(stmt)

    async def wip_amounts(
        self,
        organization_id: uuid.UUID,
        as_at: date,
        require_approval: bool,
        client_ids: Collection[uuid.UUID],
    ) -> dict[uuid.UUID, int]:
        """The WIP of each of these clients as at ``as_at``."""
        if not client_ids:
            return {}
        items = wip_items(organization_id, as_at, require_approval, ItemFilter())
        stmt = (
            select(items.c.client_id, total(items.c.time_value + items.c.disbursement_value))
            .where(items.c.client_id.in_(list(client_ids)))
            .group_by(items.c.client_id)
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all()}

    # --- Debtors --------------------------------------------------------------------------

    async def debtor_breakdown(self, organization_id: uuid.UUID, as_at: date) -> RowMapping:
        """One row: the outstanding balance and the number of invoices in each age bucket
        (``<bucket>`` and ``n_<bucket>``), the total, the invoices and the clients."""
        balances = invoice_balances(organization_id, as_at, ItemFilter())
        amounts = debt_buckets(balances.c.due_date, balances.c.balance, as_at)
        counts = debt_buckets(balances.c.due_date, literal(1), as_at)
        stmt = select(
            *(expr.label(name) for name, expr in amounts.items()),
            *(expr.label(f"n_{name}") for name, expr in counts.items()),
            total(balances.c.balance).label("total"),
            func.count().label("invoice_count"),
            func.count(distinct(balances.c.client_id)).label("debtor_count"),
        ).where(balances.c.balance > 0)
        return (await self.session.execute(stmt)).mappings().one()

    async def top_debtors(
        self, organization_id: uuid.UUID, as_at: date, limit: int
    ) -> Sequence[RowMapping]:
        """Clients with a balance, the largest overdue balance first, with their age buckets."""
        balances = invoice_balances(organization_id, as_at, ItemFilter())
        overdue = balances.c.due_date < as_at
        stmt = (
            select(
                Client.id.label("client_id"),
                Client.code.label("client_code"),
                Client.name.label("client_name"),
                func.count().label("invoice_count"),
                total(balances.c.balance).label("total"),
                total(when(overdue, balances.c.balance)).label("overdue"),
                func.min(case((overdue, balances.c.due_date))).label("oldest_due_date"),
                *(
                    expr.label(name)
                    for name, expr in debt_buckets(
                        balances.c.due_date, balances.c.balance, as_at
                    ).items()
                ),
            )
            .select_from(balances)
            .join(
                Client,
                and_(Client.organization_id == organization_id, Client.id == balances.c.client_id),
            )
            .where(balances.c.balance > 0)
            .group_by(Client.id, Client.code, Client.name)
            .order_by(desc("overdue"), desc("total"), func.lower(Client.name), Client.id)
            .limit(limit)
        )
        return await self._rows(stmt)
