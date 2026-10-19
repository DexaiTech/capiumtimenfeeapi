"""Missing timesheets and reminders. HTTP only: rules live in the services."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.time import MAX_TIME_DATE, MIN_TIME_DATE
from app.schemas.time_views import MissingTimesheet, RemindersResult, RemindersRun
from app.services.timesheet_reminders import TimesheetReminderService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Timesheets"])


def get_reminder_service(session: SessionDep) -> TimesheetReminderService:
    return TimesheetReminderService(session)


ReminderServiceDep = Annotated[TimesheetReminderService, Depends(get_reminder_service)]


@router.get(
    "/timesheets/missing",
    summary="Missing timesheets",
    description=(
        "Staff whose timesheet for a week is not submitted, sorted by name: `status` `none` (no "
        "timesheet), `open` (not submitted) or `rejected` (returned), the minutes logged and "
        "expected, the working days up to today with no time, and when a reminder was last "
        "sent. Listed are active staff with a weekly capacity above 0 who are employed on a "
        "working day of the week. `week_start` may be any day of the week (default: the last "
        "completed week). Everyone with `time.read_all`; otherwise the people the caller "
        "manages (an empty list when none). `user_id` (`me` or a user id) and "
        "`manager_user_id` (the people that manager approves) narrow it."
    ),
    responses=error_responses(403, 404),
)
async def missing_timesheets(
    org: OrgContextDep,
    service: ReminderServiceDep,
    week_start: Annotated[
        date | None,
        Query(
            description="Any day of the week (default: the last completed week)",
            ge=MIN_TIME_DATE,
            le=MAX_TIME_DATE,
        ),
    ] = None,
    user_id: Annotated[str | None, Query(description="'me' or one person's user id")] = None,
    manager_user_id: Annotated[
        uuid.UUID | None, Query(description="Only the people this manager approves")
    ] = None,
) -> ApiResponse[list[MissingTimesheet]]:
    return ok(
        await service.missing(
            org, week_start=week_start, user_id=user_id, manager_user_id=manager_user_id
        )
    )


@router.post(
    "/timesheets/reminders/run",
    summary="Send timesheet reminders",
    description=(
        "Sends the timesheet reminder to staff whose timesheet for the week (`week_start`, "
        "default the last completed week) is missing, open or returned, and one reminder to "
        "each approver with submitted timesheets of that week or earlier. A person is "
        "reminded once per week: running it again only reaches people not reminded yet. "
        "Returns the counts (`reminded`, `already_reminded`, `unreachable` for people with no "
        "login, `approvers_reminded`, `disabled` notification types). Requires "
        "`organization.update` or `timesheet.approve`; practice-wide with `organization.update` "
        "or `time.read_all`, otherwise for the people the caller manages. The same run is "
        "scheduled daily by the worker for every practice."
    ),
    responses=error_responses(403),
)
async def run_reminders(
    org: OrgContextDep,
    service: ReminderServiceDep,
    ctx: ContextDep,
    body: Annotated[RemindersRun | None, Body()] = None,
) -> ApiResponse[RemindersResult]:
    result = await service.run(org, body or RemindersRun(), ctx)
    return ok(result, f"{result.reminded + result.approvers_reminded} reminders sent")
