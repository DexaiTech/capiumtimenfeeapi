"""The job calendar: the jobs that overlap a date range and the tasks due in it."""

import uuid
from collections.abc import Sequence
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.db.models import Job
from app.repositories.job_budgets import JobBudgetRepository, JobUsage
from app.repositories.job_calendar import CalendarFilters, JobCalendarRepository
from app.repositories.job_tasks import TaskRepository
from app.repositories.time_jobs import ClientJobRepository
from app.repositories.time_refs import TimeReferenceRepository
from app.schemas.job_calendar import CalendarEvent, JobCalendar
from app.schemas.job_tasks import AssigneeRef
from app.services.policy import OrgContext
from app.services.time_jobs import resolve_user_ref
from app.utils.job_rules import OPEN_JOB_STATUSES, usage_bp, worst
from app.utils.money import percentage_bp

MAX_RANGE_DAYS = 366
MAX_EVENTS = 5000
JOB_STATUSES = ("planned", "in_progress", "on_hold", "completed", "closed", "cancelled")
TASK_STATUSES = ("not_started", "in_progress", "on_hold", "completed")


def job_span(job: Job) -> tuple[date, date | None]:
    """The days a job is drawn on: start date to due date. With one date it is that day; with a
    start date and no due date it has no end while it is open (and is its start day once it is
    finished). A job without dates has no span."""
    start = job.start_date or job.due_date
    if start is None:
        raise ValueError("a job without dates is not on the calendar")
    if job.due_date is not None:
        return start, job.due_date
    return start, None if job.status in OPEN_JOB_STATUSES else job.start_date


def status_filters(
    statuses: Sequence[str] | None,
) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None]:
    """The job statuses and the task statuses a ``status`` filter selects (None: any).
    ``planned`` and ``not_started`` mean the same."""
    if statuses is None:
        return None, None
    jobs = tuple(dict.fromkeys("planned" if s == "not_started" else s for s in statuses))
    tasks = tuple(dict.fromkeys("not_started" if s == "planned" else s for s in statuses))
    return (
        tuple(s for s in jobs if s in JOB_STATUSES),
        tuple(s for s in tasks if s in TASK_STATUSES),
    )


class JobCalendarService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.calendar_repo = JobCalendarRepository(session)
        self.jobs = ClientJobRepository(session)
        self.tasks = TaskRepository(session)
        self.budgets = JobBudgetRepository(session)
        self.refs = TimeReferenceRepository(session)

    async def calendar(
        self,
        ctx: OrgContext,
        *,
        start: date,
        end: date,
        client_id: uuid.UUID | None,
        manager_user_id: str | None,
        assignee_user_id: str | None,
        statuses: Sequence[str] | None,
    ) -> JobCalendar:
        if end < start:
            raise ValidationError.for_field("to", "The range cannot end before it starts.")
        if end - start > timedelta(days=MAX_RANGE_DAYS):
            raise ValidationError.for_field(
                "to", f"The range can span at most {MAX_RANGE_DAYS} days.", "range_too_long"
            )
        org_id = ctx.organization_id
        job_statuses, task_statuses = status_filters(statuses)
        filters = CalendarFilters(
            client_id=client_id,
            manager_user_id=resolve_user_ref(ctx, manager_user_id, "manager_user_id"),
            assignee_user_id=resolve_user_ref(ctx, assignee_user_id, "assignee_user_id"),
            job_statuses=job_statuses,
            task_statuses=task_statuses,
        )
        jobs = (
            []
            if job_statuses == ()
            else await self.calendar_repo.jobs(org_id, start, end, filters, MAX_EVENTS + 1)
        )
        tasks = (
            []
            if task_statuses == ()
            else await self.calendar_repo.tasks(org_id, start, end, filters, MAX_EVENTS + 1)
        )
        if len(jobs) + len(tasks) > MAX_EVENTS:
            raise ValidationError.for_field(
                "to",
                "There are too many jobs and tasks in this range: narrow the dates or filter.",
                "too_many_events",
            )

        job_ids = [row.job.id for row in jobs]
        task_ids = [view.task.id for view in tasks]
        job_people = await self.jobs.assignees(org_id, job_ids)
        task_people = await self.tasks.assignees(org_id, task_ids)
        names = await self.refs.user_names(
            org_id, {u for users in (*job_people.values(), *task_people.values()) for u in users}
        )
        usage = await self.budgets.usage(org_id, job_ids)
        task_actuals = await self.tasks.actuals(org_id, task_ids)

        def people(users: list[uuid.UUID]) -> list[AssigneeRef]:
            return [AssigneeRef(user_id=u, full_name=names.get(u)) for u in users]

        events: list[CalendarEvent] = []
        for row in jobs:
            job = row.job
            first, last = job_span(job)
            events.append(
                CalendarEvent(
                    type="job",
                    id=job.id,
                    job_id=job.id,
                    job_number=job.number,
                    title=job.name,
                    client_id=job.client_id,
                    client_name=row.client_name,
                    start=first,
                    end=last,
                    status=job.status,
                    due=job.due_date,
                    assignees=people(job_people.get(job.id, [])),
                    budget_used_bp=_job_used_bp(job, usage[job.id]),
                )
            )
        for view in tasks:
            task = view.task
            assert task.due_date is not None
            minutes = task_actuals[task.id].minutes
            events.append(
                CalendarEvent(
                    type="task",
                    id=task.id,
                    job_id=task.job_id,
                    job_number=view.job_number,
                    title=task.title,
                    client_id=view.client_id,
                    client_name=view.client_name,
                    start=task.due_date,
                    end=task.due_date,
                    status=task.status,
                    due=task.due_date,
                    assignees=people(task_people.get(task.id, [])),
                    budget_used_bp=(
                        percentage_bp(minutes, task.budget_minutes) if task.budget_minutes else None
                    ),
                )
            )
        events.sort(key=lambda e: (e.start, e.type != "job", e.title.lower(), str(e.id)))
        return JobCalendar(range_from=start, range_to=end, events=events)


def _job_used_bp(job: Job, usage: JobUsage) -> int | None:
    return worst(
        usage_bp(usage.logged_minutes, job.budget_minutes),
        usage_bp(usage.time_value, job.budget_fee),
    )
