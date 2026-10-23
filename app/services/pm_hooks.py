"""What the jobs module asks of the practice management (PM) integration: three small hooks.

* ``ensure_pm_fields_free``: the name, service, manager, partner and dates of a job linked to PM
  belong to PM, so changing them here answers 409 ``JOB_FIELD_MANAGED_BY_PM`` (like
  ``CLIENT_FIELD_MANAGED_CENTRALLY`` for a linked client). Saying the value it already has is fine.
* ``queue_job_status`` / ``queue_task_status``: a status change of a linked job or task queues one
  outbound sync item (the latest status wins), which the runner pushes to PM. Nothing is called
  while PM's own change is being applied (``applying_pm_change``), so a change that came from PM
  is not echoed back; nothing is queued for a job that is not linked or while status sync is off.

A leaf module: the job and task services import it, so it imports neither of them.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.db.models import Job, Task
from app.repositories.integration_items import ConnectionRepository
from app.services.integration_outbox import enqueue_latest
from app.services.integrations import PRACTICE_MANAGEMENT
from app.utils.ids import new_id

PM_OWNED_JOB_FIELDS = (
    "name",
    "service_id",
    "manager_user_id",
    "partner_user_id",
    "start_date",
    "due_date",
)
_FIELD_NAMES = {
    "name": "name",
    "service_id": "service",
    "manager_user_id": "manager",
    "partner_user_id": "partner",
    "start_date": "start date",
    "due_date": "due date",
}
_applying_pm_change: ContextVar[bool] = ContextVar("applying_pm_change", default=False)


@contextmanager
def applying_pm_change() -> Iterator[None]:
    """While active, changes to linked jobs are PM's own: the PM-owned fields may be written and
    no status is queued for PM."""
    token = _applying_pm_change.set(True)
    try:
        yield
    finally:
        _applying_pm_change.reset(token)


def applying_pm_change_active() -> bool:
    """Whether PM's own change is being applied (its fields may be written)."""
    return _applying_pm_change.get()


def ensure_pm_fields_free(job: Job, data: BaseModel) -> None:
    """409 ``JOB_FIELD_MANAGED_BY_PM`` when a request would change a PM-owned field of a linked
    job (the details name each field)."""
    if job.pm_job_id is None or _applying_pm_change.get():
        return
    sent = data.model_fields_set
    managed = [
        field
        for field in PM_OWNED_JOB_FIELDS
        if field in sent and getattr(data, field) != getattr(job, field)
    ]
    if managed:
        raise ConflictError(
            "The name, service, manager, partner and dates of a job linked to practice "
            "management can only be changed there.",
            code="JOB_FIELD_MANAGED_BY_PM",
            details=[
                {
                    "field": field,
                    "code": "managed_by_pm",
                    "message": f"The {_FIELD_NAMES[field]} is managed in practice management.",
                }
                for field in managed
            ],
        )


async def _status_sync_on(session: AsyncSession, organization_id: uuid.UUID) -> bool:
    connection = await ConnectionRepository(session).get(organization_id, PRACTICE_MANAGEMENT)
    return (
        connection is not None
        and connection.enabled
        and bool(connection.config.get("sync_statuses", True))
    )


async def queue_job_status(session: AsyncSession, job: Job) -> None:
    """Queue the job's new status for PM (a linked job only, while status sync is on)."""
    if job.pm_job_id is None or _applying_pm_change.get():
        return
    if not await _status_sync_on(session, job.organization_id):
        return
    await enqueue_latest(
        session,
        job.organization_id,
        PRACTICE_MANAGEMENT,
        operation="push_job_status",
        idempotency_key=f"job_status:{job.id}",
        entity_type="job",
        entity_id=job.id,
        payload={
            "label": f"{job.number} {job.name}",
            "pm_job_id": job.pm_job_id,
            "status": job.status,
            "change_id": str(new_id()),
        },
    )


async def queue_task_status(session: AsyncSession, job: Job, task: Task) -> None:
    """Queue the task's new status for PM (a linked task of a linked job only)."""
    if job.pm_job_id is None or task.pm_task_id is None or _applying_pm_change.get():
        return
    if not await _status_sync_on(session, job.organization_id):
        return
    await enqueue_latest(
        session,
        job.organization_id,
        PRACTICE_MANAGEMENT,
        operation="push_task_status",
        idempotency_key=f"task_status:{task.id}",
        entity_type="task",
        entity_id=task.id,
        payload={
            "label": f"{job.number} {task.title}",
            "pm_job_id": job.pm_job_id,
            "pm_task_id": task.pm_task_id,
            "status": task.status,
            "change_id": str(new_id()),
        },
    )
