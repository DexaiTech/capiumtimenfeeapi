"""Job (client work) queries. Every method is scoped to one organization.

Named ``time_jobs`` because ``app/repositories/jobs.py`` is the background-job queue.
"""

import uuid
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Date,
    Select,
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
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Client,
    Disbursement,
    Expense,
    Invoice,
    InvoiceLine,
    Job,
    JobAssignee,
    Service,
    TimeEntry,
    WipAllocation,
    WipWriteOff,
)
from app.repositories.time_paging import keyset_finish, keyset_query, parse_date
from app.utils.pagination import Page, PageParams

FAR_FUTURE = date(9999, 12, 31)


@dataclass(frozen=True, slots=True)
class JobView:
    job: Job
    client_code: str
    client_name: str
    service_name: str | None


@dataclass(slots=True)
class JobActuals:
    logged_minutes: int = 0
    billable_minutes: int = 0
    time_value: int = 0
    wip_value: int = 0
    billed_value: int = 0
    written_off_value: int = 0


@dataclass(frozen=True, slots=True)
class JobFilters:
    client_id: uuid.UUID | None = None
    statuses: Sequence[str] | None = None
    manager_user_id: uuid.UUID | None = None
    partner_user_id: uuid.UUID | None = None
    assignee_user_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    due_from: date | None = None
    due_to: date | None = None
    q: str | None = None


def _view_select() -> Select[*tuple[Any, ...]]:
    return (
        select(Job, Client.code, Client.name, Service.name)
        .join(
            Client, and_(Client.organization_id == Job.organization_id, Client.id == Job.client_id)
        )
        .outerjoin(
            Service,
            and_(Service.organization_id == Job.organization_id, Service.id == Job.service_id),
        )
        .execution_options(populate_existing=True)
    )


def _parse_datetime(value: Any) -> datetime:
    return datetime.fromisoformat(str(value))


def _sort(sort: str) -> tuple[Any, bool, Callable[[Any], Any], Callable[[Job], Any]]:
    """``(expression, descending, cursor parser, value of a row)`` for a sort key."""
    descending = sort.startswith("-")
    key = sort.lstrip("-")
    if key == "due_date":
        expr: ColumnElement[Any] = func.coalesce(Job.due_date, cast(literal(FAR_FUTURE), Date))
        return expr, descending, parse_date, lambda j: j.due_date or FAR_FUTURE
    if key == "number":
        return Job.number, descending, str, lambda j: j.number
    return Job.created_at, descending, _parse_datetime, lambda j: j.created_at.isoformat()


class ClientJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, job: Job) -> Job:
        self.session.add(job)
        await self.session.flush()
        return job

    async def get(
        self, organization_id: uuid.UUID, job_id: uuid.UUID, *, for_update: bool = False
    ) -> Job | None:
        """A live (not deleted) job."""
        stmt = select(Job).where(
            Job.organization_id == organization_id, Job.id == job_id, Job.deleted_at.is_(None)
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_no_key(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> Job | None:
        """A live job locked ``FOR NO KEY UPDATE``.

        Writers of the job's tasks and budget alerts queue behind each other and behind a
        status change (``FOR UPDATE``), while time writes, which hold ``FOR KEY SHARE`` on the
        job, are not held up.
        """
        stmt = (
            select(Job)
            .where(
                Job.organization_id == organization_id,
                Job.id == job_id,
                Job.deleted_at.is_(None),
            )
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_many(
        self,
        organization_id: uuid.UUID,
        job_ids: Collection[uuid.UUID],
        *,
        key_share: bool = False,
    ) -> dict[uuid.UUID, Job]:
        """Live jobs by id.

        ``key_share=True`` takes ``FOR KEY SHARE`` locks (in id order) for work being added
        to the jobs (time, timers): a concurrent status change or delete (which locks the
        job ``FOR UPDATE``) either waits for this transaction or is seen here first, so time
        never lands on a job that was closed or deleted at the same moment.
        """
        if not job_ids:
            return {}
        stmt = select(Job).where(
            Job.organization_id == organization_id,
            Job.id.in_(list(job_ids)),
            Job.deleted_at.is_(None),
        )
        if key_share:
            stmt = (
                stmt.order_by(Job.id)
                .with_for_update(read=True, key_share=True)
                .execution_options(populate_existing=True)
            )
        return {j.id: j for j in (await self.session.execute(stmt)).scalars()}

    async def lock_many(
        self, organization_id: uuid.UUID, job_ids: Collection[uuid.UUID]
    ) -> list[Job]:
        """Live jobs among ``job_ids`` locked ``FOR UPDATE`` in id order (no deadlocks)."""
        stmt = (
            select(Job)
            .where(
                Job.organization_id == organization_id,
                Job.id.in_(list(job_ids)),
                Job.deleted_at.is_(None),
            )
            .order_by(Job.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def view(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> JobView | None:
        stmt = _view_select().where(
            Job.organization_id == organization_id, Job.id == job_id, Job.deleted_at.is_(None)
        )
        row = (await self.session.execute(stmt)).one_or_none()
        return JobView(row[0], row[1], row[2], row[3]) if row else None

    async def list_page(
        self, organization_id: uuid.UUID, filters: JobFilters, sort: str, params: PageParams
    ) -> Page[JobView]:
        stmt = _view_select().where(
            Job.organization_id == organization_id, Job.deleted_at.is_(None)
        )
        if filters.client_id is not None:
            stmt = stmt.where(Job.client_id == filters.client_id)
        if filters.statuses:
            stmt = stmt.where(Job.status.in_(list(filters.statuses)))
        if filters.manager_user_id is not None:
            stmt = stmt.where(Job.manager_user_id == filters.manager_user_id)
        if filters.partner_user_id is not None:
            stmt = stmt.where(Job.partner_user_id == filters.partner_user_id)
        if filters.service_id is not None:
            stmt = stmt.where(Job.service_id == filters.service_id)
        if filters.due_from is not None:
            stmt = stmt.where(Job.due_date >= filters.due_from)
        if filters.due_to is not None:
            stmt = stmt.where(Job.due_date <= filters.due_to)
        if filters.assignee_user_id is not None:
            stmt = stmt.where(
                exists().where(
                    JobAssignee.organization_id == Job.organization_id,
                    JobAssignee.job_id == Job.id,
                    JobAssignee.user_id == filters.assignee_user_id,
                )
            )
        if filters.q:
            stmt = stmt.where(
                or_(
                    Job.name.icontains(filters.q, autoescape=True),
                    Job.number.icontains(filters.q, autoescape=True),
                    Client.name.icontains(filters.q, autoescape=True),
                )
            )
        expr, descending, parse, value_of = _sort(sort)
        stmt = keyset_query(
            stmt,
            sort_expr=expr,
            id_column=Job.id,
            params=params,
            descending=descending,
            parse=parse,
        )
        rows = [JobView(r[0], r[1], r[2], r[3]) for r in (await self.session.execute(stmt)).all()]
        items, next_cursor = keyset_finish(rows, params, lambda v: (value_of(v.job), v.job.id))
        return Page(items=items, next_cursor=next_cursor)

    async def assignees(
        self, organization_id: uuid.UUID, job_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, list[uuid.UUID]]:
        if not job_ids:
            return {}
        stmt = (
            select(JobAssignee.job_id, JobAssignee.user_id)
            .where(
                JobAssignee.organization_id == organization_id,
                JobAssignee.job_id.in_(list(job_ids)),
            )
            .order_by(JobAssignee.job_id, JobAssignee.created_at, JobAssignee.user_id)
        )
        found: dict[uuid.UUID, list[uuid.UUID]] = {}
        for job_id, user_id in (await self.session.execute(stmt)).all():
            found.setdefault(job_id, []).append(user_id)
        return found

    async def set_assignees(
        self, organization_id: uuid.UUID, job_id: uuid.UUID, user_ids: Sequence[uuid.UUID]
    ) -> None:
        """Replace the job's assignees (keeping the rows of people who stay)."""
        keep = list(user_ids)
        await self.session.execute(
            delete(JobAssignee).where(
                JobAssignee.organization_id == organization_id,
                JobAssignee.job_id == job_id,
                JobAssignee.user_id.not_in(keep) if keep else literal(True),
            )
        )
        current = (await self.assignees(organization_id, [job_id])).get(job_id, [])
        for user_id in keep:
            if user_id not in current:
                self.session.add(
                    JobAssignee(organization_id=organization_id, job_id=job_id, user_id=user_id)
                )
        await self.session.flush()

    async def in_use(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> bool:
        """Whether time, disbursements, expense lines, invoice lines or write-offs reference the
        job."""
        checks = []
        for model in (TimeEntry, Disbursement, Expense, InvoiceLine, WipWriteOff):
            checks.append(
                exists().where(
                    model.organization_id == organization_id,
                    model.job_id == job_id,
                )
            )
        return bool(await self.session.scalar(select(or_(*checks))))

    async def in_use_ids(
        self, organization_id: uuid.UUID, job_ids: Collection[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Of ``job_ids``, the jobs that ``in_use`` is true for."""
        found: set[uuid.UUID] = set()
        for model in (TimeEntry, Disbursement, Expense, InvoiceLine, WipWriteOff):
            stmt = select(model.job_id).where(
                model.organization_id == organization_id, model.job_id.in_(list(job_ids))
            )
            found.update(j for j in (await self.session.execute(stmt)).scalars() if j)
        return found

    async def soft_delete(self, job: Job) -> None:
        job.deleted_at = datetime.now(UTC)
        await self.session.flush()

    async def actuals(
        self, organization_id: uuid.UUID, job_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, JobActuals]:
        """Logged time, WIP, billed and written-off values per job."""
        ids = list(job_ids)
        found: dict[uuid.UUID, JobActuals] = {job_id: JobActuals() for job_id in ids}
        if not ids:
            return found
        billable_minutes = case((TimeEntry.is_billable, TimeEntry.minutes), else_=0)
        billable_value = case((TimeEntry.is_billable, TimeEntry.charge_value), else_=0)
        wip_value = case(
            (
                and_(TimeEntry.is_billable, TimeEntry.billing_status == "unbilled"),
                TimeEntry.charge_value,
            ),
            else_=0,
        )
        time_stmt = (
            select(
                TimeEntry.job_id,
                func.coalesce(func.sum(TimeEntry.minutes), 0),
                func.coalesce(func.sum(billable_minutes), 0),
                func.coalesce(func.sum(billable_value), 0),
                func.coalesce(func.sum(wip_value), 0),
            )
            .where(TimeEntry.organization_id == organization_id, TimeEntry.job_id.in_(ids))
            .group_by(TimeEntry.job_id)
        )
        for job_id, logged, billable, value, wip in (await self.session.execute(time_stmt)).all():
            row = found[job_id]
            row.logged_minutes, row.billable_minutes = int(logged), int(billable)
            row.time_value, row.wip_value = int(value), int(wip)

        billed = case(
            (
                and_(WipAllocation.invoice_id.is_not(None), Invoice.status == "issued"),
                WipAllocation.billed_value,
            ),
            else_=0,
        )
        written_off = case(
            (WipAllocation.write_off_id.is_not(None), WipAllocation.wip_value), else_=0
        )
        for item_model, item_column in (
            (TimeEntry, WipAllocation.time_entry_id),
            (Disbursement, WipAllocation.disbursement_id),
        ):
            alloc_stmt = (
                select(
                    item_model.job_id,
                    func.coalesce(func.sum(billed), 0),
                    func.coalesce(func.sum(written_off), 0),
                )
                .select_from(WipAllocation)
                .join(
                    item_model,
                    and_(
                        item_model.organization_id == WipAllocation.organization_id,
                        item_model.id == item_column,
                    ),
                )
                .outerjoin(
                    Invoice,
                    and_(
                        Invoice.organization_id == WipAllocation.organization_id,
                        Invoice.id == WipAllocation.invoice_id,
                    ),
                )
                .where(
                    WipAllocation.organization_id == organization_id,
                    WipAllocation.reversed_at.is_(None),
                    item_model.job_id.in_(ids),
                )
                .group_by(item_model.job_id)
            )
            for job_id, billed_sum, written_sum in (await self.session.execute(alloc_stmt)).all():
                found[job_id].billed_value += int(billed_sum)
                found[job_id].written_off_value += int(written_sum)
        return found
