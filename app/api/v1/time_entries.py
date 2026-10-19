"""Time entries, the weekly grid and the running timer. HTTP only."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, PageParamsDep, require
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.time import (
    MAX_TIME_DATE,
    MIN_TIME_DATE,
    BillingStatus,
    TimeEntryBulkCreate,
    TimeEntryCreate,
    TimeEntryRead,
    TimeEntryUpdate,
    TimerRead,
    TimerStart,
    TimerStop,
    TimesheetStatus,
    WeekGrid,
)
from app.services.policy import OrgContext
from app.services.time_entries import TimeEntryService
from app.services.timers import TimerService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Time"])


def get_time_service(session: SessionDep) -> TimeEntryService:
    return TimeEntryService(session)


def get_timer_service(session: SessionDep) -> TimerService:
    return TimerService(session)


TimeServiceDep = Annotated[TimeEntryService, Depends(get_time_service)]
TimerServiceDep = Annotated[TimerService, Depends(get_timer_service)]

_WRITE_RULES = (
    " Own time needs `time.log`, anyone else's `time.manage_all`. Minutes are rounded up to "
    "the practice time increment; a person logs at most 24 hours a day (422 "
    "`DAY_LIMIT_EXCEEDED`). Time on a submitted/approved timesheet or that is not `unbilled` "
    "answers 409 `TIME_LOCKED`; on a completed/cancelled job 409 `JOB_CLOSED`; for an "
    "inactive/archived client 409 `CLIENT_INACTIVE`. Rates are snapshotted from the rate "
    "hierarchy; `cost_rate`/`cost_value` are null without `rate.read`. `internal_note` (up to "
    "2000 characters) is never printed on an invoice and is null for everyone but the "
    "person, `time.read_all` and `time.manage_all`; `service_id`/`service_name` are the "
    "job's service (read-only)."
)


# --- Time entries --------------------------------------------------------------------------


@router.get(
    "/time-entries",
    summary="List time entries",
    description=(
        "Newest first, cursor-paginated. `user_id`: `me` (default), a user id or `all` "
        "(others need `time.read_all`). Filters: `date_from`/`date_to` (inclusive), "
        "`client_id`, `job_id`, `billable`, `billing_status` and `timesheet_status` (repeat "
        "for several)."
    ),
    responses=error_responses(403, 404),
)
async def list_time_entries(
    org: OrgContextDep,
    service: TimeServiceDep,
    page: PageParamsDep,
    user_id: Annotated[str | None, Query(description="'me' (default), a user id or 'all'")] = None,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
    client_id: Annotated[uuid.UUID | None, Query()] = None,
    job_id: Annotated[uuid.UUID | None, Query()] = None,
    billable: Annotated[bool | None, Query()] = None,
    billing_status: Annotated[list[BillingStatus] | None, Query()] = None,
    timesheet_status: Annotated[list[TimesheetStatus] | None, Query()] = None,
) -> ApiResponse[list[TimeEntryRead]]:
    result = await service.list_entries(
        org,
        user_id=user_id,
        date_from=date_from,
        date_to=date_to,
        client_id=client_id,
        job_id=job_id,
        billable=billable,
        billing_statuses=billing_status,
        timesheet_statuses=timesheet_status,
        params=page,
    )
    return ok_page(result.items, result.next_cursor)


@router.get(
    "/time-entries/week",
    summary="Weekly time grid",
    description=(
        "A person's week as a grid: one row per client/job/activity/billable combination "
        "with minutes per day, day totals, chargeable (billable) and non-chargeable totals "
        "and the timesheet status (`open` when no sheet exists yet). `week_start` may be "
        "any day; it is moved back to the practice's first day of the week (default: this "
        "week). The person's `targets` (`daily_minutes` per day, `weekly_minutes`), "
        "`working_days`, `missing_days` (working days up to today with no time) and "
        "`day_variance` (logged minus target, per day) come with it. `user_id`: `me` "
        "(default) or a user id (needs `time.read_all` or being their manager)."
    ),
    responses=error_responses(403, 404),
)
async def week_grid(
    org: OrgContextDep,
    service: TimeServiceDep,
    week_start: Annotated[
        date | None,
        Query(description="Any day of the week", ge=MIN_TIME_DATE, le=MAX_TIME_DATE),
    ] = None,
    user_id: Annotated[str | None, Query(description="'me' (default) or a user id")] = None,
) -> ApiResponse[WeekGrid]:
    return ok(await service.week(org, week_start=week_start, user_id=user_id))


@router.post(
    "/time-entries",
    status_code=status.HTTP_201_CREATED,
    summary="Log time",
    description=(
        "Creates a time entry on the person's timesheet for that week (created on demand). "
        "A job implies its client. `is_billable` defaults to false for non-billable jobs, "
        "non-chargeable activity codes and entries without a client." + _WRITE_RULES
    ),
    responses=error_responses(403, 404, 409),
)
async def create_time_entry(
    body: TimeEntryCreate, org: OrgContextDep, service: TimeServiceDep, ctx: ContextDep
) -> ApiResponse[TimeEntryRead]:
    return ok(await service.create(org, body, ctx), "Time logged")


@router.post(
    "/time-entries/bulk",
    status_code=status.HTTP_201_CREATED,
    summary="Log many time entries",
    description=(
        "Creates up to 100 entries atomically (the weekly grid's save): one invalid entry "
        "rolls back all of them, and its error names the field as `entries.<index>.<field>`."
        + _WRITE_RULES
    ),
    responses=error_responses(403, 404, 409),
)
async def bulk_create_time_entries(
    body: TimeEntryBulkCreate, org: OrgContextDep, service: TimeServiceDep, ctx: ContextDep
) -> ApiResponse[list[TimeEntryRead]]:
    entries = await service.bulk_create(org, body, ctx)
    return ok(entries, f"{len(entries)} entries logged")


@router.get(
    "/time-entries/{entry_id}",
    summary="Get a time entry",
    description="One entry. Another person's needs `time.read_all`.",
    responses=error_responses(403, 404),
)
async def get_time_entry(
    entry_id: uuid.UUID, org: OrgContextDep, service: TimeServiceDep
) -> ApiResponse[TimeEntryRead]:
    return ok(await service.get(org, entry_id))


@router.patch(
    "/time-entries/{entry_id}",
    summary="Edit a time entry",
    description=(
        "Changes an unbilled entry on an open or rejected timesheet; rates and values are "
        "re-taken. Moving it to another week moves it to that week's timesheet." + _WRITE_RULES
    ),
    responses=error_responses(403, 404, 409),
)
async def update_time_entry(
    entry_id: uuid.UUID,
    body: TimeEntryUpdate,
    org: OrgContextDep,
    service: TimeServiceDep,
    ctx: ContextDep,
) -> ApiResponse[TimeEntryRead]:
    return ok(await service.update(org, entry_id, body, ctx), "Time entry updated")


@router.delete(
    "/time-entries/{entry_id}",
    summary="Delete a time entry",
    description=(
        "Deletes an unbilled entry on an open or rejected timesheet (409 `TIME_LOCKED` "
        "otherwise). Own entries need `time.log`, others' `time.manage_all`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_time_entry(
    entry_id: uuid.UUID, org: OrgContextDep, service: TimeServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.delete(org, entry_id, ctx)
    return ok(message="Time entry deleted")


# --- Timer ---------------------------------------------------------------------------------


TimerOrg = Annotated[OrgContext, Depends(require("time.log"))]


@router.get(
    "/timer",
    summary="Get my timer",
    description="The caller's timer with its elapsed seconds, or null. Requires `time.log`.",
    responses=error_responses(403, 404),
)
async def get_timer(org: TimerOrg, service: TimerServiceDep) -> ApiResponse[TimerRead | None]:
    return ok(await service.get(org))


@router.post(
    "/timer/start",
    status_code=status.HTTP_201_CREATED,
    summary="Start my timer",
    description=(
        "Starts the caller's timer, optionally on a client, job and activity. One per person: "
        "409 `TIMER_RUNNING` when one exists, unless `replace: true`, which first stops it "
        "(writing its entry). The client and job must accept time (409 `CLIENT_INACTIVE`, "
        "`JOB_CLOSED`). Requires `time.log`."
    ),
    responses=error_responses(403, 404, 409),
)
async def start_timer(
    body: TimerStart, org: TimerOrg, service: TimerServiceDep, ctx: ContextDep
) -> ApiResponse[TimerRead]:
    return ok(await service.start(org, body, ctx), "Timer started")


@router.post(
    "/timer/pause",
    summary="Pause my timer",
    description="409 `TIMER_NOT_RUNNING` when already paused; 404 without a timer.",
    responses=error_responses(403, 404, 409),
)
async def pause_timer(org: TimerOrg, service: TimerServiceDep) -> ApiResponse[TimerRead]:
    return ok(await service.pause(org), "Timer paused")


@router.post(
    "/timer/resume",
    summary="Resume my timer",
    description="409 `TIMER_RUNNING` when it is running; 404 without a timer.",
    responses=error_responses(403, 404, 409),
)
async def resume_timer(org: TimerOrg, service: TimerServiceDep) -> ApiResponse[TimerRead]:
    return ok(await service.resume(org), "Timer resumed")


@router.post(
    "/timer/stop",
    status_code=status.HTTP_201_CREATED,
    summary="Stop my timer",
    description=(
        "Writes a time entry for the elapsed time (rounded up to the practice increment, at "
        "least one increment) on the day the timer was started, deletes the timer and "
        "returns the entry. The entry follows every time rule (409 `TIME_LOCKED`, ...); on "
        "an error the timer is kept. An optional `description` replaces the timer's."
    ),
    responses=error_responses(403, 404, 409),
)
async def stop_timer(
    org: TimerOrg,
    service: TimerServiceDep,
    ctx: ContextDep,
    body: Annotated[TimerStop | None, Body()] = None,
) -> ApiResponse[TimeEntryRead]:
    return ok(await service.stop(org, body, ctx), "Timer stopped")


@router.delete(
    "/timer",
    summary="Discard my timer",
    description="Deletes the timer without writing an entry. 404 without a timer.",
    responses=error_responses(403, 404),
)
async def discard_timer(org: TimerOrg, service: TimerServiceDep) -> ApiResponse[None]:
    await service.discard(org)
    return ok(message="Timer discarded")
