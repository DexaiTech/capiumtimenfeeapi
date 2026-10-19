"""Day and calendar views, copying and corrections of time. HTTP only: rules live in services."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, PageParamsDep
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.time import MAX_TIME_DATE, MIN_TIME_DATE, TimeEntryRead
from app.schemas.time_views import (
    CopyEntryRequest,
    CopyWeekRequest,
    CopyWeekResult,
    TimeCalendar,
    TimeCorrection,
    TimeDay,
    TimeEntryHistoryRead,
)
from app.services.time_copy import TimeCopyService
from app.services.time_corrections import TimeCorrectionService
from app.services.time_views import TimeViewService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Time"])


def get_view_service(session: SessionDep) -> TimeViewService:
    return TimeViewService(session)


def get_copy_service(session: SessionDep) -> TimeCopyService:
    return TimeCopyService(session)


def get_correction_service(session: SessionDep) -> TimeCorrectionService:
    return TimeCorrectionService(session)


ViewServiceDep = Annotated[TimeViewService, Depends(get_view_service)]
CopyServiceDep = Annotated[TimeCopyService, Depends(get_copy_service)]
CorrectionServiceDep = Annotated[TimeCorrectionService, Depends(get_correction_service)]

_VIEW_ACCESS = (
    " `user_id`: `me` (default) or a user id (needs `time.read_all` or being their manager)."
)
_COPY_RULES = (
    " Each copy is a new entry and follows every rule of one: minutes rounded up to the practice "
    "time increment, at most 24 hours a day (`DAY_LIMIT_EXCEEDED`), no time on a completed or "
    "cancelled job (`JOB_CLOSED`) or for an inactive client (`CLIENT_INACTIVE`), no time in a "
    "locked period (`PERIOD_LOCKED`) and an editable timesheet. Rates are taken again for the "
    "new date. Own time needs `time.log`, anyone else's `time.manage_all`."
)


@router.get(
    "/time-entries/day",
    summary="Day view",
    description=(
        "One person's day: its entries, the day's total, billable and non-billable minutes, "
        "the target (0 on a day off), the minutes remaining to it and the variance (logged "
        "minus target). `timer` is the caller's own timer when they view their own day, else "
        "null." + _VIEW_ACCESS
    ),
    responses=error_responses(403, 404),
)
async def day_view(
    org: OrgContextDep,
    service: ViewServiceDep,
    day: Annotated[
        date,
        Query(alias="date", description="The day", ge=MIN_TIME_DATE, le=MAX_TIME_DATE),
    ],
    user_id: Annotated[str | None, Query(description="'me' (default) or a user id")] = None,
) -> ApiResponse[TimeDay]:
    return ok(await service.day(org, day=day, user_id=user_id))


@router.get(
    "/time-entries/calendar",
    summary="Calendar view",
    description=(
        "One row per day of `from`..`to` (inclusive, at most 62 days): the day's total and "
        "billable minutes, its target, whether it is a working day and the status of the "
        "timesheet of its week (`open` when the week has none yet)." + _VIEW_ACCESS
    ),
    responses=error_responses(403, 404),
)
async def calendar_view(
    org: OrgContextDep,
    service: ViewServiceDep,
    date_from: Annotated[
        date,
        Query(alias="from", description="First day", ge=MIN_TIME_DATE, le=MAX_TIME_DATE),
    ],
    date_to: Annotated[
        date,
        Query(
            alias="to",
            description="Last day (at most 62 days in all)",
            ge=MIN_TIME_DATE,
            le=MAX_TIME_DATE,
        ),
    ],
    user_id: Annotated[str | None, Query(description="'me' (default) or a user id")] = None,
) -> ApiResponse[TimeCalendar]:
    return ok(await service.calendar(org, date_from=date_from, date_to=date_to, user_id=user_id))


@router.post(
    "/time-entries/copy-week",
    summary="Copy a week",
    description=(
        "Copies the person's entries of `from_week_start`'s week into `to_week_start`'s week "
        "(both moved back to the practice's first day of the week), shifted by the whole-week "
        "offset; `entry_ids` limits it to some entries, `include_notes` copies the internal "
        "notes. `user_id` copies another person's week (`time.manage_all`). Partial success is "
        "normal: the answer is 200 with `created` entries and `skipped` ones "
        "(`source_entry_id`, `code`, `message`). A target week whose timesheet is submitted or "
        "approved answers 409 `TIMESHEET_NOT_EDITABLE`; the same week as the source is 422."
        + _COPY_RULES
    ),
    responses=error_responses(403, 404, 409),
)
async def copy_week(
    body: CopyWeekRequest, org: OrgContextDep, service: CopyServiceDep, ctx: ContextDep
) -> ApiResponse[CopyWeekResult]:
    result = await service.copy_week(org, body, ctx)
    return ok(result, f"{len(result.created)} entries copied")


@router.post(
    "/time-entries/{entry_id}/copy",
    status_code=status.HTTP_201_CREATED,
    summary="Copy a time entry",
    description=(
        "Copies one entry to another day (`date`; the same day is allowed) for the same "
        "person; `include_notes` copies the internal note. Returns the new entry." + _COPY_RULES
    ),
    responses=error_responses(403, 404, 409),
)
async def copy_entry(
    entry_id: uuid.UUID,
    body: CopyEntryRequest,
    org: OrgContextDep,
    service: CopyServiceDep,
    ctx: ContextDep,
) -> ApiResponse[TimeEntryRead]:
    return ok(await service.copy_entry(org, entry_id, body, ctx), "Time entry copied")


@router.post(
    "/time-entries/{entry_id}/correct",
    summary="Correct approved time",
    description=(
        "Corrects an entry whose timesheet is `approved` and which is not billed (409 "
        "`TIME_NOT_CORRECTABLE` otherwise; submitted time is returned by the approver with a "
        "reason, open or returned time is edited). Needs `time.manage_all` or "
        "`timesheet.approve`, and not one's own time unless `organization.update` (403 "
        "`SELF_CORRECTION_FORBIDDEN`). `reason` is required (422 "
        "`TIME_CORRECTION_REASON_REQUIRED`); send at least one of `date`, `minutes`, "
        "`client_id`, `job_id`, `activity_code_id`, `description`, `billable`, `internal_note`. "
        "The rules of an edit apply (increment, 24 hours a day, closed job, locked period); the "
        "rate is kept unless the client, job, date or activity changes. The entry may move to "
        "another week and then joins that week's timesheet. One history row (before and after) "
        "and the audit entry `time_entry.corrected` are written. Returns the corrected entry."
    ),
    responses=error_responses(403, 404, 409),
)
async def correct_entry(
    entry_id: uuid.UUID,
    body: TimeCorrection,
    org: OrgContextDep,
    service: CorrectionServiceDep,
    ctx: ContextDep,
) -> ApiResponse[TimeEntryRead]:
    return ok(await service.correct(org, entry_id, body, ctx), "Time corrected")


@router.get(
    "/time-entries/{entry_id}/history",
    summary="Corrections of a time entry",
    description=(
        "The corrections of an entry, newest first, cursor-paginated: the reason, who made "
        "it, the entry before and after and the fields that changed. Cost figures are null "
        "without `rate.read` and the internal note is hidden like on the entry. The person, "
        "their manager or anyone with `time.read_all`; still readable after the entry is deleted."
    ),
    responses=error_responses(403, 404),
)
async def entry_history(
    entry_id: uuid.UUID, org: OrgContextDep, service: CorrectionServiceDep, page: PageParamsDep
) -> ApiResponse[list[TimeEntryHistoryRead]]:
    result = await service.history_of(org, entry_id, page)
    return ok_page(result.items, result.next_cursor)
