"""Tasks: the work inside a job, with their own assignees, hours budget, due date and status.
HTTP only: the rules live in TaskService."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.schemas.bulk import BulkChangeResult
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.job_tasks import (
    TaskAssigneesSet,
    TaskBulkAction,
    TaskBulkCreate,
    TaskCreate,
    TaskListItem,
    TaskRead,
    TaskReorder,
    TasksFromService,
    TaskSort,
    TaskStatus,
    TaskStatusChange,
    TaskUpdate,
)
from app.services.job_tasks import TaskService
from app.services.job_tasks_bulk import TaskBulkService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Tasks"])


def get_task_service(session: SessionDep) -> TaskService:
    return TaskService(session)


def get_task_bulk_service(
    session: SessionDep, service: Annotated[TaskService, Depends(get_task_service)]
) -> TaskBulkService:
    return TaskBulkService(session, service)


TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
TaskBulkServiceDep = Annotated[TaskBulkService, Depends(get_task_bulk_service)]

_CLOSED = (
    "A closed or cancelled job answers 409 `JOB_CLOSED`: its tasks cannot be changed "
    "until it is reopened."
)


@router.post(
    "/tasks/bulk",
    summary="Change or delete several tasks",
    description=(
        "Applies one `action` (`set_status`, `set_due_date`, `add_assignee`, `remove_assignee`, "
        "`delete`) to 1-200 tasks, of any jobs, in one transaction: all or nothing. `value` is "
        "the status, an ISO date (null clears the due date), or the assignee's staff id. Each "
        "task goes through the rules of its single endpoint (a closed job, the job's dates, "
        "active assignees, time recorded against a task that stops its deletion): if any is "
        "refused the answer is 409 `BULK_REFUSED` with every task in `details` (`name`, "
        "`error_code`, `reason`, `task_id`, `job_id`) and nothing changes. An unknown id answers "
        "404 listing it. A task's own assignees may change its status; every other action needs "
        "`job.update`. `affected` counts distinct tasks, `changed` those that actually changed. "
        "Requires `job.read`."
    ),
    responses=error_responses(403, 404, 409),
)
async def bulk_tasks(
    body: TaskBulkAction,
    org: Annotated[OrgContext, Depends(require("job.read"))],
    service: TaskBulkServiceDep,
    ctx: ContextDep,
) -> ApiResponse[BulkChangeResult]:
    result = await service.run(org, body, ctx)
    return ok(result, f"{result.changed} task(s) updated")


@router.get(
    "/tasks",
    summary="List tasks across jobs",
    description=(
        "The tasks of every live job, cursor-paginated (the Assignments page). Filters: "
        "`assignee_user_id` (a user id or `me`), `status` (repeat for several), `job_id`, "
        "`client_id`, `due_from`/`due_to` (inclusive) and `q` (task title, job name or number, "
        "or client name). `sort`: `due_date` (tasks without one last), `title`, `job` (job "
        "number) or `status` (not started, in progress, on hold, completed); prefix `-` for "
        "descending. Default `due_date`. Each task carries its job and client, assignees and "
        "logged minutes. Requires `job.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_tasks(
    org: Annotated[OrgContext, Depends(require("job.read"))],
    service: TaskServiceDep,
    page: PageParamsDep,
    assignee_user_id: Annotated[
        str | None, Query(description="Only tasks assigned to this user id or 'me'")
    ] = None,
    task_status: Annotated[
        list[TaskStatus] | None,
        Query(alias="status", description="Only these statuses; repeat for several"),
    ] = None,
    job_id: Annotated[uuid.UUID | None, Query(description="Only this job's tasks")] = None,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client's jobs")] = None,
    due_from: Annotated[date | None, Query(description="Due on or after this date")] = None,
    due_to: Annotated[date | None, Query(description="Due on or before this date")] = None,
    q: Annotated[
        str | None,
        Query(max_length=200, description="Task title, job name or number, or client name"),
    ] = None,
    sort: Annotated[TaskSort, Query(description="Sort key; prefix - for descending")] = "due_date",
) -> ApiResponse[list[TaskListItem]]:
    result = await service.list_all(
        org,
        assignee_user_id=assignee_user_id,
        statuses=task_status,
        job_id=job_id,
        client_id=client_id,
        due_from=due_from,
        due_to=due_to,
        q=q,
        sort=sort,
        params=page,
    )
    return ok_page(result.items, result.next_cursor)


@router.get(
    "/jobs/{job_id}/tasks",
    summary="List a job's tasks",
    description=(
        "Every task of the job in its order (`sort_order`), with assignees, `logged_minutes`, "
        "`budget_used_bp` and `is_overdue`. "
        "Optional `status` filter (repeat for several). Not paginated: a job has at most 500 "
        "tasks. Requires `job.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_job_tasks(
    job_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require("job.read"))],
    service: TaskServiceDep,
    task_status: Annotated[
        list[TaskStatus] | None,
        Query(alias="status", description="Only these statuses; repeat for several"),
    ] = None,
) -> ApiResponse[list[TaskRead]]:
    return ok(await service.list_for_job(org, job_id, task_status))


@router.post(
    "/jobs/{job_id}/tasks/bulk",
    status_code=status.HTTP_201_CREATED,
    summary="Add several tasks to a job",
    description=(
        "Adds 1-100 tasks at the end of the job's list, in the order sent, all or nothing. Each "
        "item is a task as in `POST /jobs/{id}/tasks`. If any is refused (a due date outside "
        "the job's dates, an inactive assignee, the 500-task limit) the answer is 409 "
        "`BULK_REFUSED` with every refused item in `details` (`name` = `Task <n>: <title>`, "
        "`row`, `error_code`, `reason`) and nothing is added. " + _CLOSED + " Requires "
        "`job.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_tasks_bulk(
    job_id: uuid.UUID,
    body: TaskBulkCreate,
    org: Annotated[OrgContext, Depends(require("job.update"))],
    service: TaskBulkServiceDep,
    ctx: ContextDep,
) -> ApiResponse[list[TaskRead]]:
    created = await service.create_many(org, job_id, body, ctx)
    return ok(created, f"{len(created)} task(s) added")


@router.post(
    "/jobs/{job_id}/tasks/from-service",
    summary="Add the service's default tasks to a job",
    description=(
        "Adds the default tasks of the job's service (`GET /services/{id}/task-templates`) that "
        "the job does not have yet: a task whose title (ignoring case) is already on the job is "
        "skipped. Each is due `due_offset_days` after the job's start date when that lies "
        "within the job's dates, and is assigned to the job's assignees. Answers 409 "
        "`SERVICE_HAS_NO_TASKS` when the job has no service or the service has no default "
        "tasks; a closed or cancelled job answers 409 `JOB_CLOSED`. `added` and `skipped` "
        "count the templates. Requires `job.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def add_tasks_from_service(
    job_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require("job.update"))],
    service: TaskBulkServiceDep,
    ctx: ContextDep,
) -> ApiResponse[TasksFromService]:
    result = await service.add_from_service(org, job_id, ctx)
    return ok(result, f"{result.added} task(s) added")


@router.post(
    "/jobs/{job_id}/tasks",
    status_code=status.HTTP_201_CREATED,
    summary="Add a task to a job",
    description=(
        "Adds a task at the end of the job's list. `status` defaults to `not_started`; "
        "`due_date` must lie within the job's start and due dates where it has them, and "
        "assignees must be active members (422). A job has at most 500 tasks (409 "
        "`TASK_LIMIT_REACHED`). " + _CLOSED + " Requires `job.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_task(
    job_id: uuid.UUID,
    body: TaskCreate,
    org: Annotated[OrgContext, Depends(require("job.update"))],
    service: TaskServiceDep,
    ctx: ContextDep,
) -> ApiResponse[TaskRead]:
    return ok(await service.create(org, job_id, body, ctx), "Task added")


@router.post(
    "/jobs/{job_id}/tasks/reorder",
    summary="Reorder a job's tasks",
    description=(
        "Puts the job's tasks in the order of `task_ids`, which must name every task of the "
        "job exactly once (422 `task_ids_mismatch` otherwise). Returns the tasks in the new "
        "order. " + _CLOSED + " Requires `job.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def reorder_tasks(
    job_id: uuid.UUID,
    body: TaskReorder,
    org: Annotated[OrgContext, Depends(require("job.update"))],
    service: TaskServiceDep,
    ctx: ContextDep,
) -> ApiResponse[list[TaskRead]]:
    return ok(await service.reorder(org, job_id, body, ctx), "Tasks reordered")


@router.patch(
    "/jobs/{job_id}/tasks/{task_id}",
    summary="Update a task",
    description=(
        "Changes the title, description, hours budget or due date (null clears the last "
        "three). The due date must lie within the job's dates (422). Status and assignees have "
        "their own endpoints. " + _CLOSED + " Requires `job.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_task(
    job_id: uuid.UUID,
    task_id: uuid.UUID,
    body: TaskUpdate,
    org: Annotated[OrgContext, Depends(require("job.update"))],
    service: TaskServiceDep,
    ctx: ContextDep,
) -> ApiResponse[TaskRead]:
    return ok(await service.update(org, job_id, task_id, body, ctx), "Task updated")


@router.post(
    "/jobs/{job_id}/tasks/{task_id}/status",
    summary="Change a task's status",
    description=(
        "Moves the task to any of `not_started`, `in_progress`, `on_hold` and `completed` "
        "(`completed_at` is set on completion and cleared otherwise; the same status is "
        "accepted and changes nothing). Allowed to anyone with `job.update` and to the task's "
        "own assignees, who need only `job.read`; anyone else gets 403. " + _CLOSED
    ),
    responses=error_responses(403, 404, 409),
)
async def change_task_status(
    job_id: uuid.UUID,
    task_id: uuid.UUID,
    body: TaskStatusChange,
    org: Annotated[OrgContext, Depends(require("job.read"))],
    service: TaskServiceDep,
    ctx: ContextDep,
) -> ApiResponse[TaskRead]:
    return ok(await service.change_status(org, job_id, task_id, body, ctx), "Task status changed")


@router.put(
    "/jobs/{job_id}/tasks/{task_id}/assignees",
    summary="Set a task's assignees",
    description=(
        "Replaces the task's assignees with `user_ids` (active members only, 422 otherwise). "
        + _CLOSED
        + " Requires `job.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def set_task_assignees(
    job_id: uuid.UUID,
    task_id: uuid.UUID,
    body: TaskAssigneesSet,
    org: Annotated[OrgContext, Depends(require("job.update"))],
    service: TaskServiceDep,
    ctx: ContextDep,
) -> ApiResponse[TaskRead]:
    return ok(await service.set_assignees(org, job_id, task_id, body, ctx), "Assignees updated")


@router.delete(
    "/jobs/{job_id}/tasks/{task_id}",
    summary="Delete a task",
    description=(
        "Deletes a task that no time is recorded against and no timer is running on (409 "
        "`TASK_IN_USE` otherwise). " + _CLOSED + " Requires `job.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_task(
    job_id: uuid.UUID,
    task_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require("job.update"))],
    service: TaskServiceDep,
    ctx: ContextDep,
) -> ApiResponse[None]:
    await service.delete(org, job_id, task_id, ctx)
    return ok(message="Task deleted")
