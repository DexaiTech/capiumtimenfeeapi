"""The job calendar. HTTP only: the rules live in JobCalendarService.

Mounted before the jobs router: ``/jobs/calendar`` must not be read as ``/jobs/{job_id}``.
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.dependencies.common import SessionDep
from app.dependencies.organization import require
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.job_calendar import CalendarStatus, JobCalendar
from app.services.job_calendar import JobCalendarService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Job calendar"])


def get_calendar_service(session: SessionDep) -> JobCalendarService:
    return JobCalendarService(session)


CalendarServiceDep = Annotated[JobCalendarService, Depends(get_calendar_service)]


@router.get(
    "/jobs/calendar",
    summary="Job calendar",
    description=(
        "The jobs that overlap `from`..`to` (inclusive, at most 366 days) and the tasks due in "
        "it, as `events` ordered by `start`. A job is drawn from its start date to its due "
        "date; with one date only it is that day, and an open job with a start date and no "
        "due date has `end` null (it runs on). Jobs without dates are left out. A task is its "
        "due date. Each event has `type` (`job` or `task`), `id`, `job_id`, `title`, client, "
        "`start`, `end`, `status`, `due`, `assignees` and `budget_used_bp`. Filters: "
        "`client_id`, `manager_user_id` and `assignee_user_id` (user ids or `me`; a task "
        "counts under its job's manager and its own assignees) and `status` (repeat for "
        "several; `planned` and `not_started` are the same, and apply to a job's or a task's own "
        "status). More than 5000 events answer 422 `too_many_events`; a longer range, 422 "
        "`range_too_long`. Requires `job.read`."
    ),
    responses=error_responses(403, 404),
)
async def job_calendar(
    org: Annotated[OrgContext, Depends(require("job.read"))],
    service: CalendarServiceDep,
    range_from: Annotated[date, Query(alias="from", description="First day of the range")],
    range_to: Annotated[date, Query(alias="to", description="Last day of the range (inclusive)")],
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client's jobs")] = None,
    manager_user_id: Annotated[
        str | None, Query(description="Jobs managed by this user id or 'me'")
    ] = None,
    assignee_user_id: Annotated[
        str | None, Query(description="Jobs and tasks assigned to this user id or 'me'")
    ] = None,
    event_status: Annotated[
        list[CalendarStatus] | None,
        Query(alias="status", description="Only these statuses; repeat for several"),
    ] = None,
) -> ApiResponse[JobCalendar]:
    return ok(
        await service.calendar(
            org,
            start=range_from,
            end=range_to,
            client_id=client_id,
            manager_user_id=manager_user_id,
            assignee_user_id=assignee_user_id,
            statuses=event_status,
        )
    )
