"""Job budget queries: usage per job, the budget list with its totals, breakdowns by person and
the alerts a job has raised. Every method is scoped to one organization."""

import uuid
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    Numeric,
    Subquery,
    and_,
    case,
    cast,
    delete,
    exists,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import Client, Job, JobAssignee, JobBudgetAlert, Service, TimeEntry, User
from app.repositories.time_jobs import FAR_FUTURE
from app.repositories.time_paging import keyset_finish, keyset_query, parse_date
from app.utils.ids import new_id
from app.utils.job_rules import AMBER_BP, RED_BP
from app.utils.pagination import Page, PageParams


@dataclass(slots=True)
class JobUsage:
    logged_minutes: int = 0
    billable_minutes: int = 0
    time_value: int = 0  # charge value of the billable time
    wip_value: int = 0  # ... of it that is not yet billed
    cost_value: int = 0


@dataclass(frozen=True, slots=True)
class PersonUsage:
    user_id: uuid.UUID
    full_name: str
    minutes: int
    billable_minutes: int
    value: int
    cost_value: int


@dataclass(frozen=True, slots=True)
class BudgetFilters:
    statuses: Sequence[str] | None = None
    client_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    partner_user_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    used_from_bp: int | None = None
    used_to_bp: int | None = None
    q: str | None = None


@dataclass(frozen=True, slots=True)
class BudgetRow:
    job: Job
    client_code: str
    client_name: str
    service_name: str | None
    manager_name: str | None
    partner_name: str | None
    usage: JobUsage


@dataclass(frozen=True, slots=True)
class BudgetTotals:
    jobs: int
    green: int
    amber: int
    red: int
    budget_minutes: int  # of the jobs with an hours budget
    minutes_basis_logged: int  # time logged on those jobs
    logged_minutes: int  # time logged on every listed job
    budget_fee: int  # of the jobs with a fee budget
    fee_basis_value: int  # time value of those jobs
    time_value: int  # of every listed job
    wip_value: int
    cost_value: int


def _time_by_job(organization_id: uuid.UUID, job_ids: Collection[uuid.UUID] | None) -> Subquery:
    """Time summed per job (every job of the practice, or just ``job_ids``)."""
    billable = TimeEntry.is_billable
    stmt = (
        select(
            TimeEntry.job_id.label("job_id"),
            func.sum(TimeEntry.minutes).label("logged_minutes"),
            func.sum(case((billable, TimeEntry.minutes), else_=0)).label("billable_minutes"),
            func.sum(case((billable, TimeEntry.charge_value), else_=0)).label("time_value"),
            func.sum(
                case(
                    (
                        and_(billable, TimeEntry.billing_status == "unbilled"),
                        TimeEntry.charge_value,
                    ),
                    else_=0,
                )
            ).label("wip_value"),
            func.sum(TimeEntry.cost_value).label("cost_value"),
        )
        .where(TimeEntry.organization_id == organization_id, TimeEntry.job_id.is_not(None))
        .group_by(TimeEntry.job_id)
    )
    if job_ids is not None:
        stmt = stmt.where(TimeEntry.job_id.in_(list(job_ids)))
    return stmt.subquery("job_time")


def _bp(actual: Any, budget: Any) -> Any:
    """``actual / budget`` in basis points, rounded half up (null without a budget). The same
    rounding as ``app.utils.money.percentage_bp`` for the non-negative amounts of a budget."""
    return case(
        (budget > 0, cast(func.round(cast(actual, Numeric) * 10_000 / budget), BigInteger)),
        else_=None,
    )


def _usage_of(row: Any) -> JobUsage:
    return JobUsage(
        logged_minutes=int(row.logged_minutes or 0),
        billable_minutes=int(row.billable_minutes or 0),
        time_value=int(row.time_value or 0),
        wip_value=int(row.wip_value or 0),
        cost_value=int(row.cost_value or 0),
    )


class JobBudgetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def usage(
        self, organization_id: uuid.UUID, job_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, JobUsage]:
        """Time logged, its value and its cost per job (zeros for a job without time)."""
        ids = list(job_ids)
        found = {job_id: JobUsage() for job_id in ids}
        if not ids:
            return found
        t = _time_by_job(organization_id, ids)
        for row in (await self.session.execute(select(t))).all():
            found[row.job_id] = _usage_of(row)
        return found

    async def by_person(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> list[PersonUsage]:
        """Who logged time on the job: most minutes first."""
        billable = TimeEntry.is_billable
        minutes = func.sum(TimeEntry.minutes)
        stmt = (
            select(
                TimeEntry.user_id,
                User.full_name,
                minutes,
                func.sum(case((billable, TimeEntry.minutes), else_=0)),
                func.sum(case((billable, TimeEntry.charge_value), else_=0)),
                func.sum(TimeEntry.cost_value),
            )
            .join(User, User.id == TimeEntry.user_id)
            .where(TimeEntry.organization_id == organization_id, TimeEntry.job_id == job_id)
            .group_by(TimeEntry.user_id, User.full_name)
            .order_by(minutes.desc(), func.lower(User.full_name), TimeEntry.user_id)
        )
        return [
            PersonUsage(row[0], row[1], int(row[2]), int(row[3]), int(row[4]), int(row[5]))
            for row in (await self.session.execute(stmt)).all()
        ]

    # --- The budget list -----------------------------------------------------------------

    async def list_page(
        self,
        organization_id: uuid.UUID,
        filters: BudgetFilters,
        sort: str,
        descending: bool,
        params: PageParams,
    ) -> Page[BudgetRow]:
        t = _time_by_job(organization_id, None)
        manager, partner = aliased(User), aliased(User)
        logged = func.coalesce(t.c.logged_minutes, 0)
        value = func.coalesce(t.c.time_value, 0)
        used = func.greatest(_bp(logged, Job.budget_minutes), _bp(value, Job.budget_fee))
        expr, parse = self._sort(sort, used, logged, value)
        stmt = self._filtered(
            select(
                Job,
                Client.code,
                Client.name,
                Service.name,
                manager.full_name,
                partner.full_name,
                logged.label("logged_minutes"),
                func.coalesce(t.c.billable_minutes, 0).label("billable_minutes"),
                value.label("time_value"),
                func.coalesce(t.c.wip_value, 0).label("wip_value"),
                func.coalesce(t.c.cost_value, 0).label("cost_value"),
                expr.label("sort_key"),
            ),
            organization_id,
            filters,
            t,
            manager,
            partner,
            used,
        ).execution_options(populate_existing=True)
        stmt = keyset_query(
            stmt,
            sort_expr=expr,
            id_column=Job.id,
            params=params,
            descending=descending,
            parse=parse,
        )
        found = (await self.session.execute(stmt)).all()
        rows = [
            BudgetRow(
                job=row[0],
                client_code=row[1],
                client_name=row[2],
                service_name=row[3],
                manager_name=row[4],
                partner_name=row[5],
                usage=_usage_of(row),
            )
            for row in found
        ]
        keys = {row[0].id: row[11] for row in found}
        items, next_cursor = keyset_finish(rows, params, lambda r: (keys[r.job.id], r.job.id))
        return Page(items=items, next_cursor=next_cursor)

    async def totals(self, organization_id: uuid.UUID, filters: BudgetFilters) -> BudgetTotals:
        """The budget list's totals over every job the filters select (not just one page)."""
        t = _time_by_job(organization_id, None)
        manager, partner = aliased(User), aliased(User)
        logged = func.coalesce(t.c.logged_minutes, 0)
        value = func.coalesce(t.c.time_value, 0)
        used = func.greatest(_bp(logged, Job.budget_minutes), _bp(value, Job.budget_fee))
        has_hours, has_fee = Job.budget_minutes > 0, Job.budget_fee > 0
        stmt = self._filtered(
            select(Job.id),
            organization_id,
            filters,
            t,
            manager,
            partner,
            used,
        ).with_only_columns(
            func.count(),
            func.count().filter(used < AMBER_BP),
            func.count().filter(and_(used >= AMBER_BP, used < RED_BP)),
            func.count().filter(used >= RED_BP),
            func.coalesce(func.sum(Job.budget_minutes).filter(has_hours), 0),
            func.coalesce(func.sum(logged).filter(has_hours), 0),
            func.coalesce(func.sum(logged), 0),
            func.coalesce(func.sum(Job.budget_fee).filter(has_fee), 0),
            func.coalesce(func.sum(value).filter(has_fee), 0),
            func.coalesce(func.sum(value), 0),
            func.coalesce(func.sum(func.coalesce(t.c.wip_value, 0)), 0),
            func.coalesce(func.sum(func.coalesce(t.c.cost_value, 0)), 0),
        )
        row = (await self.session.execute(stmt)).one()
        return BudgetTotals(*(int(v) for v in row))

    @staticmethod
    def _sort(sort: str, used: Any, logged: Any, value: Any) -> tuple[Any, Callable[[Any], Any]]:
        if sort == "number":
            return Job.number, str
        if sort == "name":
            return func.lower(Job.name), str
        if sort == "client":
            return func.lower(Client.name), str
        if sort == "due_date":
            return func.coalesce(Job.due_date, cast(literal(FAR_FUTURE), Date)), parse_date
        if sort == "logged_minutes":
            return logged, int
        if sort == "time_value":
            return value, int
        return used, int

    @staticmethod
    def _filtered(
        stmt: Any,
        organization_id: uuid.UUID,
        filters: BudgetFilters,
        t: Subquery,
        manager: Any,
        partner: Any,
        used: Any,
    ) -> Any:
        """Jobs that have a budget, joined to what the list shows, narrowed by the filters."""
        stmt = (
            stmt.select_from(Job)
            .join(
                Client,
                and_(Client.organization_id == Job.organization_id, Client.id == Job.client_id),
            )
            .outerjoin(
                Service,
                and_(Service.organization_id == Job.organization_id, Service.id == Job.service_id),
            )
            .outerjoin(manager, manager.id == Job.manager_user_id)
            .outerjoin(partner, partner.id == Job.partner_user_id)
            .outerjoin(t, t.c.job_id == Job.id)
            .where(
                Job.organization_id == organization_id,
                Job.deleted_at.is_(None),
                or_(Job.budget_minutes > 0, Job.budget_fee > 0),
            )
        )
        if filters.statuses:
            stmt = stmt.where(Job.status.in_(list(filters.statuses)))
        if filters.client_id is not None:
            stmt = stmt.where(Job.client_id == filters.client_id)
        if filters.manager_user_id is not None:
            stmt = stmt.where(Job.manager_user_id == filters.manager_user_id)
        if filters.partner_user_id is not None:
            stmt = stmt.where(Job.partner_user_id == filters.partner_user_id)
        if filters.service_id is not None:
            stmt = stmt.where(Job.service_id == filters.service_id)
        if filters.used_from_bp is not None:
            stmt = stmt.where(used >= filters.used_from_bp)
        if filters.used_to_bp is not None:
            stmt = stmt.where(used <= filters.used_to_bp)
        if filters.q:
            stmt = stmt.where(
                or_(
                    Job.name.icontains(filters.q, autoescape=True),
                    Job.number.icontains(filters.q, autoescape=True),
                    Client.name.icontains(filters.q, autoescape=True),
                )
            )
        return stmt


class JobBudgetAlertRepository:
    """The thresholds each job has crossed (``job_budget_alerts``)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def for_jobs(
        self, organization_id: uuid.UUID, job_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, list[JobBudgetAlert]]:
        if not job_ids:
            return {}
        stmt = (
            select(JobBudgetAlert)
            .where(
                JobBudgetAlert.organization_id == organization_id,
                JobBudgetAlert.job_id.in_(list(job_ids)),
            )
            .order_by(
                JobBudgetAlert.triggered_at,
                JobBudgetAlert.kind,
                JobBudgetAlert.threshold,
            )
            .execution_options(populate_existing=True)
        )
        found: dict[uuid.UUID, list[JobBudgetAlert]] = {}
        for alert in (await self.session.execute(stmt)).scalars():
            found.setdefault(alert.job_id, []).append(alert)
        return found

    async def add(
        self,
        organization_id: uuid.UUID,
        job_id: uuid.UUID,
        kind: str,
        threshold: int,
        usage_bp: int,
    ) -> uuid.UUID | None:
        """Record the crossing; None when the threshold is already recorded."""
        stmt = (
            pg_insert(JobBudgetAlert)
            .values(
                id=new_id(),
                organization_id=organization_id,
                job_id=job_id,
                kind=kind,
                threshold=threshold,
                usage_bp=usage_bp,
            )
            .on_conflict_do_nothing(
                index_elements=["organization_id", "job_id", "kind", "threshold"]
            )
            .returning(JobBudgetAlert.id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def remove(
        self, organization_id: uuid.UUID, job_id: uuid.UUID, kind: str, threshold: int
    ) -> None:
        await self.session.execute(
            delete(JobBudgetAlert).where(
                JobBudgetAlert.organization_id == organization_id,
                JobBudgetAlert.job_id == job_id,
                JobBudgetAlert.kind == kind,
                JobBudgetAlert.threshold == threshold,
            )
        )

    async def assignee_ids(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = (
            select(JobAssignee.user_id)
            .where(JobAssignee.organization_id == organization_id, JobAssignee.job_id == job_id)
            .order_by(JobAssignee.created_at, JobAssignee.user_id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def has_any(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> bool:
        stmt = select(
            exists().where(
                JobBudgetAlert.organization_id == organization_id, JobBudgetAlert.job_id == job_id
            )
        )
        return bool(await self.session.scalar(stmt))
