"""Tasks: the work inside a job, with their own assignees, hours budget, due date and status.

Every write takes the job ``FOR NO KEY UPDATE`` first: writers of one job's tasks queue (the task
limit, the sort order and the closed check are read under that lock) and a job being closed at
the same moment is seen, while time recording (which holds ``FOR KEY SHARE`` on the job) is not
held up.
"""

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError, ValidationError
from app.db.models import Job, Task
from app.db.session import unit_of_work
from app.repositories.job_tasks import TaskActuals, TaskFilters, TaskRepository
from app.repositories.time_jobs import ClientJobRepository
from app.repositories.time_refs import TimeReferenceRepository
from app.schemas.job_tasks import (
    AssigneeRef,
    TaskAssigneesSet,
    TaskCreate,
    TaskListItem,
    TaskRead,
    TaskReorder,
    TaskStatusChange,
    TaskUpdate,
)
from app.services import audit
from app.services.common import apply_changes, audit_excerpt
from app.services.pm_hooks import queue_task_status
from app.services.policy import OrgContext, has_permission
from app.services.time_jobs import ensure_active_assignees, resolve_user_ref
from app.services.time_rules import JobClosedError, practice_now, practice_today
from app.utils.pagination import Page, PageParams

MAX_TASKS_PER_JOB = 500
# Tasks cannot be added, changed or removed on a job in one of these statuses.
LOCKED_JOB_STATUSES = ("closed", "cancelled")


class TaskInUseError(ConflictError):
    code = "TASK_IN_USE"
    message = (
        "Time is recorded against this task, or a timer is running on it: it cannot be deleted."
    )


class TaskLimitError(ConflictError):
    code = "TASK_LIMIT_REACHED"
    message = f"A job can have at most {MAX_TASKS_PER_JOB} tasks."


class TaskService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.tasks = TaskRepository(session)
        self.jobs = ClientJobRepository(session)
        self.refs = TimeReferenceRepository(session)

    # --- Reads ---------------------------------------------------------------------------

    async def list_for_job(
        self, ctx: OrgContext, job_id: uuid.UUID, statuses: Sequence[str] | None
    ) -> list[TaskRead]:
        if await self.jobs.get(ctx.organization_id, job_id) is None:
            raise NotFoundError("Job not found.")
        tasks = await self.tasks.list_for_job(ctx.organization_id, job_id, statuses)
        return await self._reads(ctx, tasks)

    async def list_all(
        self,
        ctx: OrgContext,
        *,
        assignee_user_id: str | None,
        statuses: Sequence[str] | None,
        job_id: uuid.UUID | None,
        client_id: uuid.UUID | None,
        due_from: date | None,
        due_to: date | None,
        q: str | None,
        sort: str,
        params: PageParams,
    ) -> Page[TaskListItem]:
        filters = TaskFilters(
            assignee_user_id=resolve_user_ref(ctx, assignee_user_id, "assignee_user_id"),
            statuses=statuses,
            job_id=job_id,
            client_id=client_id,
            due_from=due_from,
            due_to=due_to,
            q=q.strip() if q and q.strip() else None,
        )
        page = await self.tasks.list_page(ctx.organization_id, filters, sort, params)
        ids = [view.task.id for view in page.items]
        people, actuals = await self._people_and_actuals(ctx, ids)
        today = practice_today(ctx.organization)
        items = [
            TaskListItem.from_view(
                view, actuals.get(view.task.id), people.get(view.task.id, []), today
            )
            for view in page.items
        ]
        return Page(items=items, next_cursor=page.next_cursor)

    async def _reads(self, ctx: OrgContext, tasks: Sequence[Task]) -> list[TaskRead]:
        people, actuals = await self._people_and_actuals(ctx, [t.id for t in tasks])
        today = practice_today(ctx.organization)
        return [
            TaskRead.build(task, actuals.get(task.id), people.get(task.id, []), today)
            for task in tasks
        ]

    async def _people_and_actuals(
        self, ctx: OrgContext, task_ids: Sequence[uuid.UUID]
    ) -> tuple[dict[uuid.UUID, list[AssigneeRef]], dict[uuid.UUID, TaskActuals]]:
        org_id = ctx.organization_id
        assigned = await self.tasks.assignees(org_id, task_ids)
        names = await self.refs.user_names(
            org_id, {u for users in assigned.values() for u in users}
        )
        people = {
            task_id: [AssigneeRef(user_id=u, full_name=names.get(u)) for u in users]
            for task_id, users in assigned.items()
        }
        return people, await self.tasks.actuals(org_id, task_ids)

    async def _read_one(self, ctx: OrgContext, task: Task) -> TaskRead:
        [read] = await self._reads(ctx, [task])
        return read

    # --- Writes --------------------------------------------------------------------------

    async def create(
        self, ctx: OrgContext, job_id: uuid.UUID, data: TaskCreate, rctx: RequestContext
    ) -> TaskRead:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            job = await self._lock_job(ctx, job_id)
            self._ensure_job_open(job)
            if await self.tasks.count_for_job(org_id, job.id) >= MAX_TASKS_PER_JOB:
                raise TaskLimitError()
            _check_due_date(job, data.due_date)
            assignees = list(dict.fromkeys(data.assignee_user_ids))
            await ensure_active_assignees(self.refs, org_id, assignees)
            task = await self.tasks.add(
                Task(
                    organization_id=org_id,
                    job_id=job.id,
                    title=data.title,
                    description=data.description or None,
                    status=data.status,
                    budget_minutes=data.budget_minutes,
                    due_date=data.due_date,
                    sort_order=await self.tasks.next_sort_order(org_id, job.id),
                    completed_at=practice_now() if data.status == "completed" else None,
                    created_by=ctx.user.id,
                )
            )
            if assignees:
                await self.tasks.set_assignees(org_id, task.id, assignees)
            await self._audit(
                ctx,
                rctx,
                "task.created",
                job,
                task,
                {
                    "status": task.status,
                    "budget_minutes": task.budget_minutes,
                    "due_date": task.due_date.isoformat() if task.due_date else None,
                    "assignee_user_ids": [str(u) for u in assignees],
                },
            )
            result = await self._read_one(ctx, task)
        return result

    async def update(
        self,
        ctx: OrgContext,
        job_id: uuid.UUID,
        task_id: uuid.UUID,
        data: TaskUpdate,
        rctx: RequestContext,
    ) -> TaskRead:
        fields = data.model_fields_set
        async with unit_of_work(self.session):
            job = await self._lock_job(ctx, job_id)
            self._ensure_job_open(job)
            task = await self._lock_task(ctx, job.id, task_id)
            if "due_date" in fields and data.due_date != task.due_date:
                _check_due_date(job, data.due_date)
            values: dict[str, Any] = {}
            for field in ("title", "description", "budget_minutes", "due_date"):
                if field in fields:
                    value = getattr(data, field)
                    values[field] = (value or None) if field == "description" else value
            changes = apply_changes(task, values)
            if changes:
                await self.session.flush()
                if "description" in changes:
                    changes["description"] = [audit_excerpt(v) for v in changes["description"]]
                await self._audit(ctx, rctx, "task.updated", job, task, {"changes": changes})
            result = await self._read_one(ctx, task)
        return result

    async def change_status(
        self,
        ctx: OrgContext,
        job_id: uuid.UUID,
        task_id: uuid.UUID,
        data: TaskStatusChange,
        rctx: RequestContext,
    ) -> TaskRead:
        """Anyone who can edit jobs, and a task's own assignees, may move it between statuses."""
        async with unit_of_work(self.session):
            job = await self._lock_job(ctx, job_id)
            task = await self._lock_task(ctx, job.id, task_id)
            if not has_permission(ctx, "job.update"):
                assigned = await self.tasks.assignees(ctx.organization_id, [task.id])
                if ctx.user.id not in assigned.get(task.id, []):
                    raise AuthorizationError()
            self._ensure_job_open(job)
            old = task.status
            if old != data.status:
                task.status = data.status
                task.completed_at = practice_now() if data.status == "completed" else None
                await self.session.flush()
                await self._audit(
                    ctx,
                    rctx,
                    "task.status_changed",
                    job,
                    task,
                    {"old": old, "new": data.status},
                )
                await queue_task_status(self.session, job, task)
            result = await self._read_one(ctx, task)
        return result

    async def set_assignees(
        self,
        ctx: OrgContext,
        job_id: uuid.UUID,
        task_id: uuid.UUID,
        data: TaskAssigneesSet,
        rctx: RequestContext,
    ) -> TaskRead:
        org_id = ctx.organization_id
        user_ids = list(dict.fromkeys(data.user_ids))
        async with unit_of_work(self.session):
            job = await self._lock_job(ctx, job_id)
            self._ensure_job_open(job)
            task = await self._lock_task(ctx, job.id, task_id)
            await ensure_active_assignees(self.refs, org_id, user_ids)
            old = (await self.tasks.assignees(org_id, [task.id])).get(task.id, [])
            await self.tasks.set_assignees(org_id, task.id, user_ids)
            if set(old) != set(user_ids):
                await self._audit(
                    ctx,
                    rctx,
                    "task.assignees_changed",
                    job,
                    task,
                    {"old": [str(u) for u in old], "new": [str(u) for u in user_ids]},
                )
            result = await self._read_one(ctx, task)
        return result

    async def delete(
        self, ctx: OrgContext, job_id: uuid.UUID, task_id: uuid.UUID, rctx: RequestContext
    ) -> None:
        async with unit_of_work(self.session):
            # The job is locked FOR KEY SHARE, not like the other writes: time and timers take
            # the task FOR KEY SHARE and then the job FOR NO KEY UPDATE (the budget alerts), so
            # a delete that held the job that way while waiting for the task could deadlock.
            found = await self.jobs.get_many(ctx.organization_id, [job_id], key_share=True)
            job = found.get(job_id)
            if job is None:
                raise NotFoundError("Job not found.")
            self._ensure_job_open(job)
            task = await self._lock_task(ctx, job.id, task_id, exclusive=True)
            if await self.tasks.in_use(ctx.organization_id, task.id):
                raise TaskInUseError()
            await self._audit(ctx, rctx, "task.deleted", job, task, {"status": task.status})
            await self.tasks.delete(task)

    async def reorder(
        self, ctx: OrgContext, job_id: uuid.UUID, data: TaskReorder, rctx: RequestContext
    ) -> list[TaskRead]:
        """Put the job's tasks in the order sent, which must name every task exactly once."""
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            job = await self._lock_job(ctx, job_id)
            self._ensure_job_open(job)
            tasks = await self.tasks.list_for_job(org_id, job.id, for_update=True)
            if len(data.task_ids) != len(tasks) or {t.id for t in tasks} != set(data.task_ids):
                raise ValidationError(
                    details=[
                        {
                            "field": "task_ids",
                            "code": "task_ids_mismatch",
                            "message": "Send every task of the job exactly once.",
                        }
                    ]
                )
            before = [t.id for t in tasks]
            by_id = {t.id: t for t in tasks}
            ordered = [by_id[task_id] for task_id in data.task_ids]
            for position, task in enumerate(ordered):
                task.sort_order = position
            await self.session.flush()
            if before != data.task_ids:
                await audit.record(
                    self.session,
                    "job.tasks_reordered",
                    ctx=rctx,
                    actor_user_id=ctx.user.id,
                    organization_id=org_id,
                    target_type="job",
                    target_id=job.id,
                    meta={
                        "number": job.number,
                        "name": job.name,
                        "client_id": str(job.client_id),
                        "order": [str(task_id) for task_id in data.task_ids],
                    },
                )
            result = await self._reads(ctx, ordered)
        return result

    # --- Helpers -------------------------------------------------------------------------

    async def _lock_job(self, ctx: OrgContext, job_id: uuid.UUID) -> Job:
        job = await self.jobs.lock_no_key(ctx.organization_id, job_id)
        if job is None:
            raise NotFoundError("Job not found.")
        return job

    async def _lock_task(
        self, ctx: OrgContext, job_id: uuid.UUID, task_id: uuid.UUID, *, exclusive: bool = False
    ) -> Task:
        task = await self.tasks.get(
            ctx.organization_id, job_id, task_id, for_update=True, exclusive=exclusive
        )
        if task is None:
            raise NotFoundError("Task not found.")
        return task

    @staticmethod
    def _ensure_job_open(job: Job) -> None:
        if job.status in LOCKED_JOB_STATUSES:
            raise JobClosedError(
                f"Tasks cannot be changed on a {job.status} job.",
                details={"job_id": str(job.id), "status": job.status},
            )

    async def _audit(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        action: str,
        job: Job,
        task: Task,
        extra: dict[str, Any],
    ) -> None:
        client = await self.refs.get_client(ctx.organization_id, job.client_id)
        await audit.record(
            self.session,
            action,
            ctx=rctx,
            actor_user_id=ctx.user.id,
            organization_id=ctx.organization_id,
            target_type="task",
            target_id=task.id,
            meta={
                "title": task.title,
                "job_id": str(job.id),
                "job_number": job.number,
                "job_name": job.name,
                "client_id": str(job.client_id),
                "client_name": client.name if client else None,
                **extra,
            },
        )


def _check_due_date(job: Job, due: date | None) -> None:
    """A task is due within the job's dates (each bound only where the job has it)."""
    if due is None:
        return
    if job.start_date is not None and due < job.start_date:
        raise ValidationError.for_field(
            "due_date",
            f"The due date cannot be before the job's start date ({job.start_date.isoformat()}).",
            "before_job_start",
        )
    if job.due_date is not None and due > job.due_date:
        raise ValidationError.for_field(
            "due_date",
            f"The due date cannot be after the job's due date ({job.due_date.isoformat()}).",
            "after_job_due",
        )
