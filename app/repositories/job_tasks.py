"""Task (the work inside a job) queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Date,
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

from app.db.models import Client, Job, Task, TaskAssignee, TimeEntry, Timer
from app.repositories.time_jobs import FAR_FUTURE
from app.repositories.time_paging import keyset_finish, keyset_query, parse_date
from app.utils.pagination import Page, PageParams

# The order of a task's workflow: the sort key of ``status``.
STATUS_RANK = {"not_started": 0, "in_progress": 1, "on_hold": 2, "completed": 3}


@dataclass(slots=True)
class TaskActuals:
    minutes: int = 0
    billable_minutes: int = 0
    value: int = 0  # charge value of the billable time
    cost_value: int = 0


@dataclass(frozen=True, slots=True)
class TaskView:
    task: Task
    job_number: str
    job_name: str
    job_status: str
    client_id: uuid.UUID
    client_name: str


@dataclass(frozen=True, slots=True)
class TaskFilters:
    assignee_user_id: uuid.UUID | None = None
    statuses: Sequence[str] | None = None
    job_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    due_from: date | None = None
    due_to: date | None = None
    q: str | None = None


def _status_rank() -> ColumnElement[int]:
    return case(*[(Task.status == k, v) for k, v in STATUS_RANK.items()], else_=len(STATUS_RANK))


def _sort(sort: str) -> tuple[Any, bool, Callable[[Any], Any]]:
    """``(expression, descending, cursor parser)`` for a sort key (``-`` prefix: descending)."""
    descending = sort.startswith("-")
    key = sort.lstrip("-")
    if key == "title":
        return func.lower(Task.title), descending, str
    if key == "job":
        return Job.number, descending, str
    if key == "status":
        return _status_rank(), descending, int
    # Tasks without a due date come last.
    return func.coalesce(Task.due_date, cast(literal(FAR_FUTURE), Date)), descending, parse_date


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Rows ----------------------------------------------------------------------------

    async def add(self, task: Task) -> Task:
        self.session.add(task)
        await self.session.flush()
        return task

    async def delete(self, task: Task) -> None:
        await self.session.delete(task)
        await self.session.flush()

    async def get(
        self,
        organization_id: uuid.UUID,
        job_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        for_update: bool = False,
        exclusive: bool = False,
    ) -> Task | None:
        """A task of that job (the same id under another job answers None).

        ``for_update`` locks it ``FOR NO KEY UPDATE``, which time being logged against the task
        (``FOR KEY SHARE``) does not wait for; ``exclusive`` (deleting it) locks it
        ``FOR UPDATE``, which waits for that time and is waited for by it.
        """
        stmt = select(Task).where(
            Task.organization_id == organization_id, Task.job_id == job_id, Task.id == task_id
        )
        if for_update or exclusive:
            stmt = stmt.with_for_update(key_share=not exclusive).execution_options(
                populate_existing=True
            )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_many(
        self,
        organization_id: uuid.UUID,
        task_ids: Collection[uuid.UUID],
        *,
        key_share: bool = False,
    ) -> dict[uuid.UUID, Task]:
        """Tasks of the practice by id, whatever their job.

        ``key_share=True`` takes ``FOR KEY SHARE`` locks (in id order) for time being logged
        against the tasks: a concurrent delete of a task (``FOR UPDATE``) either waits for this
        transaction or has already removed the task, so time never lands on a vanishing task.
        """
        if not task_ids:
            return {}
        stmt = select(Task).where(
            Task.organization_id == organization_id, Task.id.in_(list(task_ids))
        )
        if key_share:
            stmt = (
                stmt.order_by(Task.id)
                .with_for_update(read=True, key_share=True)
                .execution_options(populate_existing=True)
            )
        return {task.id: task for task in (await self.session.execute(stmt)).scalars()}

    async def list_for_job(
        self,
        organization_id: uuid.UUID,
        job_id: uuid.UUID,
        statuses: Sequence[str] | None = None,
        *,
        for_update: bool = False,
    ) -> list[Task]:
        """The job's tasks in their order (``sort_order``, then oldest first)."""
        stmt = (
            select(Task)
            .where(Task.organization_id == organization_id, Task.job_id == job_id)
            .order_by(Task.sort_order, Task.created_at, Task.id)
        )
        if statuses:
            stmt = stmt.where(Task.status.in_(list(statuses)))
        if for_update:
            stmt = stmt.with_for_update(key_share=True).execution_options(populate_existing=True)
        return list((await self.session.execute(stmt)).scalars())

    async def next_sort_order(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> int:
        stmt = select(func.coalesce(func.max(Task.sort_order), -1) + 1).where(
            Task.organization_id == organization_id, Task.job_id == job_id
        )
        return int(await self.session.scalar(stmt) or 0)

    async def count_for_job(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(
            Task.organization_id == organization_id, Task.job_id == job_id
        )
        return int(await self.session.scalar(stmt) or 0)

    async def counts(
        self, organization_id: uuid.UUID, job_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[int, int]]:
        """``(total, completed)`` per job; jobs without tasks are left out."""
        if not job_ids:
            return {}
        stmt = (
            select(
                Task.job_id,
                func.count(),
                func.count().filter(Task.status == "completed"),
            )
            .where(Task.organization_id == organization_id, Task.job_id.in_(list(job_ids)))
            .group_by(Task.job_id)
        )
        return {row[0]: (int(row[1]), int(row[2])) for row in (await self.session.execute(stmt))}

    async def outside_job_dates(
        self,
        organization_id: uuid.UUID,
        job_id: uuid.UUID,
        start: date | None,
        due: date | None,
    ) -> list[Task]:
        """Open tasks whose due date falls before ``start`` or after ``due``."""
        outside = []
        if start is not None:
            outside.append(Task.due_date < start)
        if due is not None:
            outside.append(Task.due_date > due)
        if not outside:
            return []
        stmt = (
            select(Task)
            .where(
                Task.organization_id == organization_id,
                Task.job_id == job_id,
                Task.status != "completed",
                Task.due_date.is_not(None),
                or_(*outside),
            )
            .order_by(Task.due_date, Task.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    # --- The cross-job list --------------------------------------------------------------

    async def list_page(
        self,
        organization_id: uuid.UUID,
        filters: TaskFilters,
        sort: str,
        params: PageParams,
    ) -> Page[TaskView]:
        expr, descending, parse = _sort(sort)
        stmt = (
            select(
                Task,
                Job.number,
                Job.name,
                Job.status,
                Job.client_id,
                Client.name,
                expr.label("sort_key"),
            )
            .join(Job, and_(Job.organization_id == Task.organization_id, Job.id == Task.job_id))
            .join(
                Client,
                and_(Client.organization_id == Job.organization_id, Client.id == Job.client_id),
            )
            .where(Task.organization_id == organization_id, Job.deleted_at.is_(None))
            .execution_options(populate_existing=True)
        )
        if filters.job_id is not None:
            stmt = stmt.where(Task.job_id == filters.job_id)
        if filters.client_id is not None:
            stmt = stmt.where(Job.client_id == filters.client_id)
        if filters.statuses:
            stmt = stmt.where(Task.status.in_(list(filters.statuses)))
        if filters.due_from is not None:
            stmt = stmt.where(Task.due_date >= filters.due_from)
        if filters.due_to is not None:
            stmt = stmt.where(Task.due_date <= filters.due_to)
        if filters.assignee_user_id is not None:
            stmt = stmt.where(
                exists().where(
                    TaskAssignee.organization_id == Task.organization_id,
                    TaskAssignee.task_id == Task.id,
                    TaskAssignee.user_id == filters.assignee_user_id,
                )
            )
        if filters.q:
            stmt = stmt.where(
                or_(
                    Task.title.icontains(filters.q, autoescape=True),
                    Job.name.icontains(filters.q, autoescape=True),
                    Job.number.icontains(filters.q, autoescape=True),
                    Client.name.icontains(filters.q, autoescape=True),
                )
            )
        stmt = keyset_query(
            stmt,
            sort_expr=expr,
            id_column=Task.id,
            params=params,
            descending=descending,
            parse=parse,
        )
        found = (await self.session.execute(stmt)).all()
        keys = {row[0].id: row[6] for row in found}
        views = [TaskView(row[0], row[1], row[2], row[3], row[4], row[5]) for row in found]
        items, next_cursor = keyset_finish(views, params, lambda v: (keys[v.task.id], v.task.id))
        return Page(items=items, next_cursor=next_cursor)

    # --- Assignees -----------------------------------------------------------------------

    async def assignees(
        self, organization_id: uuid.UUID, task_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, list[uuid.UUID]]:
        if not task_ids:
            return {}
        stmt = (
            select(TaskAssignee.task_id, TaskAssignee.user_id)
            .where(
                TaskAssignee.organization_id == organization_id,
                TaskAssignee.task_id.in_(list(task_ids)),
            )
            .order_by(TaskAssignee.task_id, TaskAssignee.created_at, TaskAssignee.user_id)
        )
        found: dict[uuid.UUID, list[uuid.UUID]] = {}
        for task_id, user_id in (await self.session.execute(stmt)).all():
            found.setdefault(task_id, []).append(user_id)
        return found

    async def set_assignees(
        self, organization_id: uuid.UUID, task_id: uuid.UUID, user_ids: Sequence[uuid.UUID]
    ) -> None:
        """Replace the task's assignees (keeping the rows of people who stay)."""
        keep = list(user_ids)
        await self.session.execute(
            delete(TaskAssignee).where(
                TaskAssignee.organization_id == organization_id,
                TaskAssignee.task_id == task_id,
                TaskAssignee.user_id.not_in(keep) if keep else literal(True),
            )
        )
        current = (await self.assignees(organization_id, [task_id])).get(task_id, [])
        for user_id in keep:
            if user_id not in current:
                self.session.add(
                    TaskAssignee(organization_id=organization_id, task_id=task_id, user_id=user_id)
                )
        await self.session.flush()

    # --- Time ----------------------------------------------------------------------------

    async def actuals(
        self, organization_id: uuid.UUID, task_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, TaskActuals]:
        """Time logged against each task (zeros for a task without time)."""
        ids = list(task_ids)
        found = {task_id: TaskActuals() for task_id in ids}
        if not ids:
            return found
        column = TimeEntry.task_id
        stmt = (
            select(
                column,
                func.coalesce(func.sum(TimeEntry.minutes), 0),
                func.coalesce(
                    func.sum(case((TimeEntry.is_billable, TimeEntry.minutes), else_=0)), 0
                ),
                func.coalesce(
                    func.sum(case((TimeEntry.is_billable, TimeEntry.charge_value), else_=0)), 0
                ),
                func.coalesce(func.sum(TimeEntry.cost_value), 0),
            )
            .where(TimeEntry.organization_id == organization_id, column.in_(ids))
            .group_by(column)
        )
        for task_id, minutes, billable, value, cost in (await self.session.execute(stmt)).all():
            found[task_id] = TaskActuals(int(minutes), int(billable), int(value), int(cost))
        return found

    async def in_use(self, organization_id: uuid.UUID, task_id: uuid.UUID) -> bool:
        """Whether time is recorded against the task or a timer is running on it."""
        stmt = select(
            or_(
                exists().where(
                    TimeEntry.organization_id == organization_id, TimeEntry.task_id == task_id
                ),
                exists().where(Timer.organization_id == organization_id, Timer.task_id == task_id),
            )
        )
        return bool(await self.session.scalar(stmt))
