"""Job calendar queries: the jobs that overlap a date range and the tasks due in it.

Every method is scoped to one organization.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, Job, JobAssignee, Task, TaskAssignee
from app.repositories.job_tasks import TaskView
from app.utils.job_rules import OPEN_JOB_STATUSES


@dataclass(frozen=True, slots=True)
class CalendarFilters:
    client_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None  # the job's manager (a task's job)
    assignee_user_id: uuid.UUID | None = None  # the job's or the task's assignee
    job_statuses: Sequence[str] | None = None  # None: any status
    task_statuses: Sequence[str] | None = None


@dataclass(frozen=True, slots=True)
class CalendarJob:
    job: Job
    client_name: str


class JobCalendarRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def jobs(
        self,
        organization_id: uuid.UUID,
        start: date,
        end: date,
        filters: CalendarFilters,
        limit: int,
    ) -> list[CalendarJob]:
        """Live jobs whose span overlaps ``start..end``.

        A job spans its start date to its due date. With one date only it is a single day;
        with a start date and no due date it runs on without an end while it is open.
        Jobs without dates are not on the calendar.
        """
        first = func.coalesce(Job.start_date, Job.due_date)
        last = func.coalesce(Job.due_date, Job.start_date)
        open_ended = and_(Job.due_date.is_(None), Job.status.in_(OPEN_JOB_STATUSES))
        stmt = (
            select(Job, Client.name)
            .join(
                Client,
                and_(Client.organization_id == Job.organization_id, Client.id == Job.client_id),
            )
            .where(
                Job.organization_id == organization_id,
                Job.deleted_at.is_(None),
                first.is_not(None),
                first <= end,
                or_(open_ended, last >= start),
            )
            .execution_options(populate_existing=True)
        )
        if filters.job_statuses is not None:
            stmt = stmt.where(Job.status.in_(list(filters.job_statuses)))
        if filters.client_id is not None:
            stmt = stmt.where(Job.client_id == filters.client_id)
        if filters.manager_user_id is not None:
            stmt = stmt.where(Job.manager_user_id == filters.manager_user_id)
        if filters.assignee_user_id is not None:
            stmt = stmt.where(
                exists().where(
                    JobAssignee.organization_id == Job.organization_id,
                    JobAssignee.job_id == Job.id,
                    JobAssignee.user_id == filters.assignee_user_id,
                )
            )
        stmt = stmt.order_by(first, func.lower(Job.name), Job.id).limit(limit)
        return [CalendarJob(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def tasks(
        self,
        organization_id: uuid.UUID,
        start: date,
        end: date,
        filters: CalendarFilters,
        limit: int,
    ) -> list[TaskView]:
        """Tasks of live jobs that are due within ``start..end``."""
        stmt = (
            select(Task, Job.number, Job.name, Job.status, Job.client_id, Client.name)
            .join(Job, and_(Job.organization_id == Task.organization_id, Job.id == Task.job_id))
            .join(
                Client,
                and_(Client.organization_id == Job.organization_id, Client.id == Job.client_id),
            )
            .where(
                Task.organization_id == organization_id,
                Job.deleted_at.is_(None),
                Task.due_date >= start,
                Task.due_date <= end,
            )
            .execution_options(populate_existing=True)
        )
        if filters.task_statuses is not None:
            stmt = stmt.where(Task.status.in_(list(filters.task_statuses)))
        if filters.client_id is not None:
            stmt = stmt.where(Job.client_id == filters.client_id)
        if filters.manager_user_id is not None:
            stmt = stmt.where(Job.manager_user_id == filters.manager_user_id)
        if filters.assignee_user_id is not None:
            stmt = stmt.where(
                exists().where(
                    TaskAssignee.organization_id == Task.organization_id,
                    TaskAssignee.task_id == Task.id,
                    TaskAssignee.user_id == filters.assignee_user_id,
                )
            )
        stmt = stmt.order_by(Task.due_date, func.lower(Task.title), Task.id).limit(limit)
        return [
            TaskView(row[0], row[1], row[2], row[3], row[4], row[5])
            for row in (await self.session.execute(stmt)).all()
        ]
