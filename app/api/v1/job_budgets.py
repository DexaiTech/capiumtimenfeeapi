"""Job budget tracking and the budget alerts. HTTP only: the rules live in JobBudgetService.

Mounted before the jobs router: ``/jobs/budgets`` must not be read as ``/jobs/{job_id}``.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.schemas.common import ApiResponse, PageMeta, error_responses, ok
from app.schemas.job_budgets import (
    BudgetEvaluationRead,
    BudgetSort,
    JobBudgetDetail,
    JobBudgetList,
    SortOrder,
)
from app.schemas.jobs import JobStatus
from app.services.job_budgets import JobBudgetService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Job budgets"])


def get_budget_service(session: SessionDep) -> JobBudgetService:
    return JobBudgetService(session)


BudgetServiceDep = Annotated[JobBudgetService, Depends(get_budget_service)]

_COST = " Cost, margin and their figures are null without `rate.read`."


@router.get(
    "/jobs/budgets",
    summary="Budget tracking",
    description=(
        "Every live job that has a budget, with budget against actual: hours (`budget_minutes`, "
        "`logged_minutes`, `budget_minutes_used_bp`) and fee (`budget_fee`, `time_value`, "
        "`wip_value`, `budget_fee_used_bp`, as on the job), `used_bp` (the worse of the two) "
        "and `rag` (`green` under 80%, `amber` from 80% to under 100%, `red` from 100%), the "
        "thresholds it has crossed (`alerts`), and a `totals` row over every job the filters "
        "select. Filters: `status` (repeat for several), `client_id`, `manager_user_id` and "
        "`partner_user_id` (user ids or `me`), `service_id`, `used_from_bp`/`used_to_bp` "
        "(inclusive) and `q` (job name or number, or client name). `sort`: `used` (default), "
        "`number`, `name`, `client`, `due_date`, `logged_minutes` or `time_value`; `order`: "
        "`asc` or `desc` (default `desc` for `used`, else `asc`). Cursor-paginated. "
        "Requires `job.read`." + _COST
    ),
    responses=error_responses(403, 404),
)
async def job_budgets(
    org: Annotated[OrgContext, Depends(require("job.read"))],
    service: BudgetServiceDep,
    page: PageParamsDep,
    job_status: Annotated[
        list[JobStatus] | None,
        Query(alias="status", description="Only these statuses; repeat for several"),
    ] = None,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client's jobs")] = None,
    manager_user_id: Annotated[
        str | None, Query(description="Jobs managed by this user id or 'me'")
    ] = None,
    partner_user_id: Annotated[
        str | None, Query(description="Jobs with this partner (user id or 'me')")
    ] = None,
    service_id: Annotated[uuid.UUID | None, Query(description="Only this service's jobs")] = None,
    used_from_bp: Annotated[
        int | None, Query(ge=0, description="Usage at least this, in basis points (8000 = 80%)")
    ] = None,
    used_to_bp: Annotated[
        int | None, Query(ge=0, description="Usage at most this, in basis points")
    ] = None,
    q: Annotated[
        str | None, Query(max_length=200, description="Job name or number, or client name")
    ] = None,
    sort: Annotated[BudgetSort, Query(description="Sort key")] = "used",
    order: Annotated[
        SortOrder | None, Query(description="asc or desc; default desc for used, else asc")
    ] = None,
) -> ApiResponse[JobBudgetList]:
    body, next_cursor = await service.list_budgets(
        org,
        statuses=job_status,
        client_id=client_id,
        manager_user_id=manager_user_id,
        partner_user_id=partner_user_id,
        service_id=service_id,
        used_from_bp=used_from_bp,
        used_to_bp=used_to_bp,
        q=q,
        sort=sort,
        order=order,
        params=page,
    )
    return ApiResponse[JobBudgetList](
        data=body, meta=PageMeta(next_cursor=next_cursor, has_more=next_cursor is not None)
    )


@router.get(
    "/jobs/{job_id}/budget",
    summary="A job's budget",
    description=(
        "Budget against actual for one job (the same figures as the budget list, plus "
        "`billed_value`, `written_off_value`, `remaining_minutes` and `remaining_fee`), the "
        "time by task (`by_task`: every task, plus a row without a task for time no task "
        "claims) and by person (`by_person`, most minutes first), and the alerts in force: "
        "the 80% and 100% thresholds of the hours and fee budgets that the job has crossed and "
        "is still above (earlier ones are in the audit log as `job.budget_alert`). "
        "Requires `job.read`." + _COST
    ),
    responses=error_responses(403, 404),
)
async def get_job_budget(
    job_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require("job.read"))],
    service: BudgetServiceDep,
) -> ApiResponse[JobBudgetDetail]:
    return ok(await service.detail(org, job_id))


@router.post(
    "/jobs/{job_id}/budget/evaluate",
    summary="Re-evaluate a job's budget alerts",
    description=(
        "Compares the job's usage with its budgets now: a threshold (80% or 100%, of the hours "
        "or fee budget) it has reached and has no alert for gets one and its manager, partner "
        "and assignees are notified once; a threshold it is back below loses its alert, so "
        "crossing it again notifies again. Alerts are raised only while the job is open "
        "(not started, in progress or on hold). Every time and disbursement write already does "
        "this; use it after changing something else, or to check. Returns `triggered`, "
        "`cleared` and the `alerts` in force. Requires `job.update`."
    ),
    responses=error_responses(403, 404),
)
async def evaluate_job_budget_alerts(
    job_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require("job.update"))],
    service: BudgetServiceDep,
    ctx: ContextDep,
) -> ApiResponse[BudgetEvaluationRead]:
    return ok(await service.evaluate(org, job_id, ctx), "Budget evaluated")
