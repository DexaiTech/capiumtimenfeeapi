"""Jobs: client work with budgets, fees and assignees. HTTP only: rules live in JobService."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.schemas.bulk import BulkChangeResult
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.jobs import (
    JobAssigneesSet,
    JobBulkAction,
    JobCopy,
    JobCreate,
    JobRead,
    JobSort,
    JobStatus,
    JobStatusChange,
    JobUpdate,
)
from app.services.policy import OrgContext
from app.services.time_jobs import JobService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Jobs"])


def get_job_service(session: SessionDep) -> JobService:
    return JobService(session)


JobServiceDep = Annotated[JobService, Depends(get_job_service)]

_ACTUALS = (
    " Each job carries its actuals: `logged_minutes`, `billable_minutes`, `time_value`, "
    "`wip_value` (unbilled billable time), `billed_value` (on issued invoices), "
    "`written_off_value` and budget usage in basis points, and `task_counts` "
    "(`total` and `completed` tasks)."
)


@router.get(
    "/jobs",
    summary="List jobs",
    description=(
        "Live jobs, cursor-paginated. Filters: `client_id`, `status` (repeat for several), "
        "`manager_user_id`/`partner_user_id`/`assignee` (a user id or `me`), `service_id`, "
        "`due_from`/`due_to` (inclusive) and `q` (job name or number, or client name). "
        "`sort`: `due_date` (jobs without a due date last), `number` or `created_at`; prefix "
        "`-` for descending. Default `-created_at`. Requires `job.read`." + _ACTUALS
    ),
    responses=error_responses(403, 404),
)
async def list_jobs(
    org: Annotated[OrgContext, Depends(require("job.read"))],
    service: JobServiceDep,
    page: PageParamsDep,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client's jobs")] = None,
    job_status: Annotated[
        list[JobStatus] | None, Query(alias="status", description="Repeat for several")
    ] = None,
    manager_user_id: Annotated[str | None, Query(description="User id or 'me'")] = None,
    partner_user_id: Annotated[str | None, Query(description="User id or 'me'")] = None,
    assignee: Annotated[str | None, Query(description="User id or 'me'")] = None,
    service_id: Annotated[uuid.UUID | None, Query()] = None,
    due_from: Annotated[date | None, Query()] = None,
    due_to: Annotated[date | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    sort: Annotated[JobSort, Query()] = "-created_at",
) -> ApiResponse[list[JobRead]]:
    result = await service.list_jobs(
        org,
        client_id=client_id,
        statuses=job_status,
        manager_user_id=manager_user_id,
        partner_user_id=partner_user_id,
        assignee=assignee,
        service_id=service_id,
        due_from=due_from,
        due_to=due_to,
        q=q,
        sort=sort,
        params=page,
    )
    return ok_page(result.items, result.next_cursor)


@router.post(
    "/jobs",
    status_code=status.HTTP_201_CREATED,
    summary="Create a job",
    description=(
        "Creates a job for a client, numbered from the `job` sequence (e.g. `J00001`). "
        "`billing_basis` defaults to the service's (else `hourly`) and a fixed-fee job's "
        "`fixed_fee` to the service's default fee; a fixed job needs a fee (422). Partner, "
        "manager and assignees must be active members (422). An inactive or archived client "
        "answers 409 `CLIENT_INACTIVE`; an unknown client or service 404. The start date "
        "cannot be after the due date (422). "
        "Requires `job.create`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_job(
    body: JobCreate,
    org: Annotated[OrgContext, Depends(require("job.create"))],
    service: JobServiceDep,
    ctx: ContextDep,
) -> ApiResponse[JobRead]:
    return ok(await service.create(org, body, ctx), "Job created")


@router.post(
    "/jobs/bulk",
    summary="Bulk action on jobs",
    description=(
        "Applies one `action` to 1-200 jobs (`job_ids`) in one transaction: all or nothing. "
        "`set_status` (`value`: a job status; each job must be allowed that transition from its "
        "status, as for `POST /jobs/{job_id}/status`), `set_manager` (`value`: a staff user id "
        "who is an active member, or null to clear the manager), `set_due_date` (`value`: "
        "YYYY-MM-DD, or null to clear it; not before a job's start date and not leaving an open "
        "task due after it) and `delete` (no `value`; same rule as `DELETE /jobs/{job_id}`). "
        "404 when any id is not a live job of the practice (`details.job_ids` lists them), 422 "
        "for a wrong `value`. A job that breaks a rule answers 409 and nothing is changed: "
        "`INVALID_STATUS_TRANSITION`, `JOB_FIELD_MANAGED_BY_PM` (name, manager and dates of a "
        "job linked to practice management), `JOB_DUE_DATE_INVALID` or `JOB_IN_USE`, with "
        "`details` listing each offending job (`job_id`, `number`, `reason`). `affected` is the "
        "number of distinct jobs selected and `changed` how many of them actually changed (a job "
        "already in the wanted state does not). Each job that changed gets its own audit row, as "
        "if it had been changed alone, with `bulk: true`. Requires `job.update` (`job.delete` "
        "for `delete`)."
    ),
    responses=error_responses(403, 404, 409),
)
async def bulk_jobs(
    body: JobBulkAction,
    org: Annotated[OrgContext, Depends(require())],
    service: JobServiceDep,
    ctx: ContextDep,
) -> ApiResponse[BulkChangeResult]:
    result = await service.bulk(org, body, ctx)
    return ok(result, f"{result.changed} job{'s' if result.changed != 1 else ''} updated")


@router.get(
    "/jobs/{job_id}",
    summary="Get a job",
    description="One job with its assignees and actuals. Requires `job.read`." + _ACTUALS,
    responses=error_responses(403, 404),
)
async def get_job(
    job_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require("job.read"))],
    service: JobServiceDep,
) -> ApiResponse[JobRead]:
    return ok(await service.get(org, job_id))


@router.patch(
    "/jobs/{job_id}",
    summary="Update a job",
    description=(
        "Changes any of the job's details except its client and status (status has its own "
        "endpoint). Changing `hourly_rate` does not re-price existing time (see "
        "`POST /rates/revalue`). The start date cannot be after the due date, and a changed "
        "date cannot leave an open task due outside the job's dates (422, "
        "`task_outside_job_dates`). Changing a budget re-evaluates the job's budget alerts. "
        "Requires `job.update`."
    ),
    responses=error_responses(403, 404),
)
async def update_job(
    job_id: uuid.UUID,
    body: JobUpdate,
    org: Annotated[OrgContext, Depends(require("job.update"))],
    service: JobServiceDep,
    ctx: ContextDep,
) -> ApiResponse[JobRead]:
    return ok(await service.update(org, job_id, body, ctx), "Job updated")


@router.post(
    "/jobs/{job_id}/status",
    summary="Change a job's status",
    description=(
        "`planned` (not started) to `in_progress`, `on_hold`, `closed` or `cancelled`; "
        "`in_progress` to `on_hold`, `completed`, `closed` or `cancelled`; `on_hold` to "
        "`in_progress`, `closed` or `cancelled`; `completed` to `closed` or back to "
        "`in_progress` (a reopen); `closed` back to `in_progress` (a reopen); `cancelled` is "
        "final. `completed_at` is set on completion and `closed_at` on closing; a reopen "
        "clears them. Any other change answers 409 `INVALID_STATUS_TRANSITION`. Completed, "
        "closed and cancelled jobs take no new time (409 `JOB_CLOSED`) and a closed or "
        "cancelled job's tasks cannot be changed. Requires `job.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def change_job_status(
    job_id: uuid.UUID,
    body: JobStatusChange,
    org: Annotated[OrgContext, Depends(require("job.update"))],
    service: JobServiceDep,
    ctx: ContextDep,
) -> ApiResponse[JobRead]:
    return ok(await service.change_status(org, job_id, body, ctx), "Job status changed")


@router.post(
    "/jobs/{job_id}/copy",
    status_code=status.HTTP_201_CREATED,
    summary="Copy a job",
    description=(
        "Creates a new job from this one, numbered from the `job` sequence, in status `planned`. "
        "The details, fees and budgets are copied (not the status, time, WIP or invoices). "
        "`name` defaults to 'Copy of ' and the source name; `client_id`, `service_id`, "
        "`start_date` and `due_date` default to the source's (send null to leave the last "
        "three empty). `copy_tasks` (default true) copies the tasks as `not_started`, keeping a "
        "task's due date when it lies within the new job's dates; `copy_assignees` (default "
        "true) copies the job's assignees and, with the tasks, theirs. A manager, partner or "
        "assignee who is no longer an active member is left out. Validated like a new job: a "
        "client that is inactive or archived answers 409 `CLIENT_INACTIVE`, an unknown "
        "client or service 404, an inactive service or a due date before the start date "
        "422. Requires `job.read` and `job.create`."
    ),
    responses=error_responses(403, 404, 409),
)
async def copy_job(
    job_id: uuid.UUID,
    body: JobCopy,
    org: Annotated[OrgContext, Depends(require("job.read", "job.create"))],
    service: JobServiceDep,
    ctx: ContextDep,
) -> ApiResponse[JobRead]:
    return ok(await service.copy(org, job_id, body, ctx), "Job copied")


@router.put(
    "/jobs/{job_id}/assignees",
    summary="Set a job's assignees",
    description=(
        "Replaces the job's assignees with `user_ids` (active members only, 422 otherwise). "
        "Requires `job.update`."
    ),
    responses=error_responses(403, 404),
)
async def set_job_assignees(
    job_id: uuid.UUID,
    body: JobAssigneesSet,
    org: Annotated[OrgContext, Depends(require("job.update"))],
    service: JobServiceDep,
    ctx: ContextDep,
) -> ApiResponse[JobRead]:
    return ok(await service.set_assignees(org, job_id, body, ctx), "Assignees updated")


@router.delete(
    "/jobs/{job_id}",
    summary="Delete a job",
    description=(
        "Deletes a job that no time, disbursement, expense line, invoice line or write-off "
        "references (409 `JOB_IN_USE` otherwise; complete or cancel it instead). Requires "
        "`job.delete`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_job(
    job_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require("job.delete"))],
    service: JobServiceDep,
    ctx: ContextDep,
) -> ApiResponse[None]:
    await service.delete(org, job_id, ctx)
    return ok(message="Job deleted")
