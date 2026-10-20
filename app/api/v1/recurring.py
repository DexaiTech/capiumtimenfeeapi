"""Recurring invoice routes. HTTP only: every rule lives in ``RecurringService``."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.repositories.recurring import ScheduleFilters
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.recurring import (
    RunRead,
    ScheduleCreate,
    ScheduleRead,
    ScheduleSummaryRead,
    ScheduleUpdate,
)
from app.services.policy import OrgContext
from app.services.recurring import RecurringService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Recurring invoices"])

SCHEDULE_STATUSES = ("active", "paused", "ended")


def get_recurring_service(session: SessionDep) -> RecurringService:
    return RecurringService(session)


RecurringServiceDep = Annotated[RecurringService, Depends(get_recurring_service)]
Reader = Annotated[OrgContext, Depends(require("recurring.read"))]
Manager = Annotated[OrgContext, Depends(require("recurring.manage"))]


@router.get(
    "/recurring",
    summary="List recurring invoice schedules",
    description=(
        "Newest first, cursor-paginated. Filters: client, `status` (repeat it: `active`, "
        "`paused`, `ended`) and `q` (name or client). Requires `recurring.read`."
    ),
    responses=error_responses(403),
)
async def list_schedules(
    org: Reader,
    service: RecurringServiceDep,
    page: PageParamsDep,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client's")] = None,
    status_: Annotated[list[str] | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(max_length=100, description="Name or client")] = None,
) -> ApiResponse[list[ScheduleSummaryRead]]:
    filters = ScheduleFilters(
        client_id=client_id,
        statuses=[s for s in status_ if s in SCHEDULE_STATUSES] if status_ else None,
        q=q.strip() if q else None,
    )
    items, next_cursor = await service.list_schedules(org, filters, page)
    return ok_page(items, next_cursor)


@router.post(
    "/recurring",
    status_code=status.HTTP_201_CREATED,
    summary="Create a recurring invoice schedule",
    description=(
        "A template of lines that raises a draft invoice on `start_date` and then every "
        "week, month, quarter or year. Nothing is issued automatically: a person reviews "
        "and issues each draft. Requires `recurring.manage`."
    ),
    responses=error_responses(403, 404),
)
async def create_schedule(
    body: ScheduleCreate, org: Manager, service: RecurringServiceDep, ctx: ContextDep
) -> ApiResponse[ScheduleRead]:
    return ok(await service.create(org, body, ctx), "Schedule created")


@router.post(
    "/recurring/runs/{run_id}/retry",
    summary="Retry a failed run",
    description=(
        "Tries the invoice of a failed run again. A run that fails again stays `failed` with "
        "its new error; one that works drafts the invoice and moves the schedule on (409 "
        "`RUN_NOT_FAILED` otherwise). Requires `recurring.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def retry_run(
    run_id: uuid.UUID, org: Manager, service: RecurringServiceDep, ctx: ContextDep
) -> ApiResponse[RunRead]:
    return ok(await service.retry_run(org, run_id, ctx))


@router.get(
    "/recurring/{schedule_id}",
    summary="Get a recurring schedule",
    responses=error_responses(403, 404),
)
async def get_schedule(
    schedule_id: uuid.UUID, org: Reader, service: RecurringServiceDep
) -> ApiResponse[ScheduleRead]:
    return ok(await service.get(org, schedule_id))


@router.patch(
    "/recurring/{schedule_id}",
    summary="Change a recurring schedule",
    description=(
        "Name, frequency, end date, reference, notes, and `lines` (which replaces every line). "
        "Changing the frequency moves the next run to the first date from today. 409 "
        "`SCHEDULE_ENDED` for an ended one. Requires `recurring.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_schedule(
    schedule_id: uuid.UUID,
    body: ScheduleUpdate,
    org: Manager,
    service: RecurringServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ScheduleRead]:
    return ok(await service.update(org, schedule_id, body, ctx), "Schedule updated")


@router.delete(
    "/recurring/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a recurring schedule",
    description=(
        "Only one that has never run (409 `SCHEDULE_HAS_RUNS`); otherwise pause it or set an "
        "end date. Requires `recurring.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_schedule(
    schedule_id: uuid.UUID, org: Manager, service: RecurringServiceDep, ctx: ContextDep
) -> Response:
    await service.delete(org, schedule_id, ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/recurring/{schedule_id}/pause",
    summary="Pause a schedule",
    description="Only an active one (409 `SCHEDULE_NOT_ACTIVE`). Requires `recurring.manage`.",
    responses=error_responses(403, 404, 409),
)
async def pause_schedule(
    schedule_id: uuid.UUID, org: Manager, service: RecurringServiceDep, ctx: ContextDep
) -> ApiResponse[ScheduleRead]:
    return ok(await service.pause(org, schedule_id, ctx), "Schedule paused")


@router.post(
    "/recurring/{schedule_id}/resume",
    summary="Resume a schedule",
    description=(
        "Only a paused one (409 `SCHEDULE_NOT_PAUSED`). Dates missed while paused are "
        "skipped: the next run is the first date from today. Requires `recurring.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def resume_schedule(
    schedule_id: uuid.UUID, org: Manager, service: RecurringServiceDep, ctx: ContextDep
) -> ApiResponse[ScheduleRead]:
    return ok(await service.resume(org, schedule_id, ctx), "Schedule resumed")


@router.get(
    "/recurring/{schedule_id}/runs",
    summary="List the runs of a schedule",
    description="Newest first by run date, cursor-paginated. Requires `recurring.read`.",
    responses=error_responses(403, 404),
)
async def list_runs(
    schedule_id: uuid.UUID, org: Reader, service: RecurringServiceDep, page: PageParamsDep
) -> ApiResponse[list[RunRead]]:
    items, next_cursor = await service.list_runs(org, schedule_id, page)
    return ok_page(items, next_cursor)
