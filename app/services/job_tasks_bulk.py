"""Several tasks at once: add many to a job, or change or delete many (docs/modules/time.md).

Every task goes through the very same ``TaskService`` call as its single endpoint, so each rule
holds here too (a closed job, the due date within the job's dates, assignees who are active
members, time recorded against a task that stops its deletion, the 500 tasks of a job). The batch
is one transaction: each item is tried inside a savepoint, the refusals are collected, and when
there is any the batch answers 409 ``BULK_REFUSED`` listing every one, with nothing changed.
"""

import uuid
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AppException, ConflictError, NotFoundError
from app.db.session import unit_of_work
from app.repositories.job_tasks import TaskRepository
from app.repositories.time_jobs import ClientJobRepository
from app.schemas.bulk import BulkChangeResult, parse_day, unique_ids
from app.schemas.job_tasks import (
    TaskAssigneesSet,
    TaskBulkAction,
    TaskBulkCreate,
    TaskRead,
    TasksFromService,
    TaskStatusChange,
    TaskUpdate,
)
from app.services import audit, policy
from app.services.job_tasks import TaskService
from app.services.policy import OrgContext
from app.services.time_rules import JobClosedError

_AUDIT_IDS_LIMIT = 100


def _refusal(label: str, exc: AppException, **extra: Any) -> dict[str, Any]:
    return {"name": label, "error_code": exc.code, "reason": exc.message, **extra}


class TaskBulkService:
    def __init__(self, session: AsyncSession, tasks: TaskService) -> None:
        self.session = session
        self.tasks = tasks
        self.repo = TaskRepository(session)
        self.jobs = ClientJobRepository(session)

    async def create_many(
        self, ctx: OrgContext, job_id: uuid.UUID, data: TaskBulkCreate, rctx: RequestContext
    ) -> list[TaskRead]:
        created: list[TaskRead] = []
        refused: list[dict[str, Any]] = []
        async with unit_of_work(self.session):
            for position, item in enumerate(data.tasks, start=1):
                try:
                    async with self.session.begin_nested():
                        created.append(await self.tasks.create(ctx, job_id, item, rctx))
                except (NotFoundError, JobClosedError):
                    # The job itself is the problem, not one of the tasks.
                    raise
                except AppException as exc:
                    refused.append(_refusal(f"Task {position}: {item.title}", exc, row=position))
            if refused:
                raise ConflictError(
                    f"{len(refused)} of {len(data.tasks)} tasks could not be added; "
                    "nothing was added.",
                    code="BULK_REFUSED",
                    details=refused,
                )
        return created

    async def add_from_service(
        self, ctx: OrgContext, job_id: uuid.UUID, rctx: RequestContext
    ) -> TasksFromService:
        """Add the job's service's default tasks that the job does not have yet (by title)."""
        from app.services.service_tasks import add_templates, templates_of

        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            job = await self.tasks._lock_job(ctx, job_id)
            self.tasks._ensure_job_open(job)
            templates = await templates_of(self.session, ctx, job.service_id)
            if not templates:
                raise ConflictError(
                    "This job has no service, or its service has no default tasks.",
                    code="SERVICE_HAS_NO_TASKS",
                )
            existing = {
                task.title.casefold() for task in await self.repo.list_for_job(org_id, job.id)
            }
            missing = [t for t in templates if t.title.casefold() not in existing]
            people = (await self.jobs.assignees(org_id, [job.id])).get(job.id, [])
            created = await add_templates(self.session, ctx, rctx, job, missing, people)
        return TasksFromService(added=len(created), skipped=len(templates) - len(missing))

    async def run(
        self, ctx: OrgContext, data: TaskBulkAction, rctx: RequestContext
    ) -> BulkChangeResult:
        # The single endpoints: edits and deletes need job.update; a status change is also open
        # to a task's own assignees, which TaskService.change_status decides per task.
        if data.action != "set_status":
            policy.ensure(ctx, "job.update")
        ids = unique_ids(data.task_ids)
        async with unit_of_work(self.session):
            found = await self.repo.get_many(ctx.organization_id, ids)
            missing = [i for i in ids if i not in found]
            if missing:
                raise NotFoundError(
                    "Some of the tasks were not found.",
                    details={"task_ids": [str(i) for i in missing]},
                )
            before = await self._states(ctx, ids)
            titles = {i: found[i].title for i in ids}
            jobs = {i: found[i].job_id for i in ids}

            refused: list[dict[str, Any]] = []
            for task_id in ids:
                try:
                    async with self.session.begin_nested():
                        await self._one(ctx, data, jobs[task_id], task_id, rctx)
                except AppException as exc:
                    refused.append(
                        _refusal(
                            titles[task_id], exc, task_id=str(task_id), job_id=str(jobs[task_id])
                        )
                    )
            if refused:
                raise ConflictError(
                    f"{len(refused)} of {len(ids)} could not be changed; nothing was changed.",
                    code="BULK_REFUSED",
                    details=refused,
                )

            if data.action == "delete":
                changed = list(ids)
            else:
                after = await self._states(ctx, ids)
                changed = [i for i in ids if after.get(i) != before.get(i)]
            if changed:
                await audit.record(
                    self.session,
                    "task.bulk_changed",
                    ctx=rctx,
                    actor_user_id=ctx.user.id,
                    organization_id=ctx.organization_id,
                    target_type="task",
                    target_id=None,
                    meta={
                        "action": data.action,
                        "count": len(changed),
                        "task_ids": [str(i) for i in changed[:_AUDIT_IDS_LIMIT]],
                        "bulk": True,
                    },
                )
        return BulkChangeResult(action=data.action, affected=len(ids), changed=len(changed))

    async def _states(
        self, ctx: OrgContext, ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[Any, ...]]:
        tasks = await self.repo.get_many(ctx.organization_id, ids)
        assigned = await self.repo.assignees(ctx.organization_id, ids)
        return {
            i: (t.status, t.due_date, tuple(sorted(str(u) for u in assigned.get(i, []))))
            for i, t in tasks.items()
        }

    async def _one(
        self,
        ctx: OrgContext,
        data: TaskBulkAction,
        job_id: uuid.UUID,
        task_id: uuid.UUID,
        rctx: RequestContext,
    ) -> None:
        match data.action:
            case "set_status":
                assert data.value is not None
                await self.tasks.change_status(
                    ctx,
                    job_id,
                    task_id,
                    TaskStatusChange.model_validate({"status": data.value}),
                    rctx,
                )
            case "set_due_date":
                due: date | None = parse_day(data.value) if data.value else None
                await self.tasks.update(ctx, job_id, task_id, TaskUpdate(due_date=due), rctx)
            case "add_assignee" | "remove_assignee":
                user = uuid.UUID(data.value or "")
                current = (await self.repo.assignees(ctx.organization_id, [task_id])).get(
                    task_id, []
                )
                if data.action == "add_assignee":
                    wanted = list(dict.fromkeys([*current, user]))
                else:
                    wanted = [u for u in current if u != user]
                if wanted != list(current):
                    await self.tasks.set_assignees(
                        ctx, job_id, task_id, TaskAssigneesSet(user_ids=wanted), rctx
                    )
            case "delete":
                await self.tasks.delete(ctx, job_id, task_id, rctx)
