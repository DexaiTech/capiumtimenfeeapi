"""The task a piece of time (an entry or a timer) is logged against.

A task belongs to one job, so time on a task needs that job. The tasks are read ``FOR KEY SHARE``
before any timer or timesheet lock is taken: deleting a task locks it ``FOR UPDATE``, so a delete
either waits for the time being logged (and then refuses, the task is in use) or has already gone
(404), and the lock order stays the same for everyone.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.db.models import Job, Task
from app.repositories.job_tasks import TaskRepository


async def load_tasks(
    session: AsyncSession, organization_id: uuid.UUID, task_ids: Iterable[uuid.UUID | None]
) -> dict[uuid.UUID, Task]:
    """The practice's tasks among ``task_ids`` (``None`` is ignored), locked ``FOR KEY SHARE``."""
    wanted = {task_id for task_id in task_ids if task_id is not None}
    return await TaskRepository(session).get_many(organization_id, wanted, key_share=True)


def task_ref(
    tasks: dict[uuid.UUID, Task], task_id: uuid.UUID | None, job: Job | None, field: str
) -> Task | None:
    """The task for ``task_id`` (``None`` for none), which must belong to ``job``.

    404 for a task that does not exist (or is another practice's), 422 for one that needs a job
    the time has not got, or belongs to another job.
    """
    if task_id is None:
        return None
    if job is None:
        raise ValidationError.for_field(
            field, "A task needs a job: choose the job the task belongs to.", "task_requires_job"
        )
    task = tasks.get(task_id)
    if task is None:
        raise NotFoundError("Task not found.", details={"field": field})
    if task.job_id != job.id:
        raise ValidationError.for_field(
            field, "This task belongs to another job.", "task_not_in_job"
        )
    return task
