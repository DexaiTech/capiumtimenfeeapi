"""A service's default tasks, added to a job (docs/modules/time.md, "Default tasks").

Used when a job is created with ``add_default_tasks`` and by ``POST /jobs/{id}/tasks/from-service``.
Every task goes through ``TaskService.create``, so each rule of adding a task holds (the 500-task
limit, a closed job, active assignees, the due date within the job's dates).
"""

import uuid
from collections.abc import Collection
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.db.models import Job, ServiceTaskTemplate
from app.repositories.settings import ServiceRepository
from app.schemas.job_tasks import TaskCreate, TaskRead
from app.services.job_tasks import TaskService
from app.services.policy import OrgContext

SOURCE = "service_template"


def due_date_for(job: Job, template: ServiceTaskTemplate) -> date | None:
    """Start date plus the offset, when the job has a start date and the result is in the job."""
    if template.due_offset_days is None or job.start_date is None:
        return None
    due = job.start_date + timedelta(days=template.due_offset_days)
    if job.due_date is not None and due > job.due_date:
        return None
    return due


async def templates_of(
    session: AsyncSession, ctx: OrgContext, service_id: uuid.UUID | None
) -> list[ServiceTaskTemplate]:
    if service_id is None:
        return []
    return await ServiceRepository(session).list_templates(ctx.organization_id, service_id)


async def add_templates(
    session: AsyncSession,
    ctx: OrgContext,
    rctx: RequestContext,
    job: Job,
    templates: list[ServiceTaskTemplate],
    assignees: Collection[uuid.UUID],
) -> list[TaskRead]:
    """One task per template, in order, inside the caller's unit of work."""
    tasks = TaskService(session)
    created: list[TaskRead] = []
    for template in templates:
        created.append(
            await tasks.create(
                ctx,
                job.id,
                TaskCreate(
                    title=template.title,
                    description=template.description,
                    budget_minutes=template.budget_minutes,
                    due_date=due_date_for(job, template),
                    assignee_user_ids=list(assignees),
                ),
                rctx,
                source=SOURCE,
            )
        )
    return created
