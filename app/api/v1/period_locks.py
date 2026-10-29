"""Period locks: choose how to lock, lock a week, month, quarter or year, unlock it, history. HTTP
only."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.period_locks import (
    PeriodLockCalendar,
    PeriodLockCreate,
    PeriodLockEventRead,
    PeriodLockRead,
    PeriodLockReopen,
    PeriodLockSettingsUpdate,
)
from app.services.period_locks import PeriodLockService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Period locks"])

Reader = Annotated[OrgContext, Depends(require("period_lock.read"))]
Locker = Annotated[OrgContext, Depends(require("period_lock.manage"))]
Reopener = Annotated[OrgContext, Depends(require("period_lock.reopen"))]


def get_period_lock_service(session: SessionDep) -> PeriodLockService:
    return PeriodLockService(session)


ServiceDep = Annotated[PeriodLockService, Depends(get_period_lock_service)]


@router.get(
    "/period-locks",
    summary="Get the period locks",
    description=(
        "The practice's locked weeks, months, quarters and years (`periods`, oldest first) and "
        "how it locks (`settings`). Time, expenses and WIP dated inside any of them cannot be "
        "created, edited or deleted (409 `PERIOD_LOCKED`). `last_event` is the latest lock or "
        "unlock. Requires `period_lock.read`."
    ),
    responses=error_responses(403, 404),
)
async def get_period_lock(org: Reader, service: ServiceDep) -> ApiResponse[PeriodLockRead]:
    return ok(await service.get(org))


@router.get(
    "/period-locks/calendar",
    summary="List the periods of an accounting year",
    description=(
        "Every period of the accounting year `year` (default the current one) of the length the "
        "practice locks (52 or 53 weeks, 12 months, 4 quarters or the year), each `open`, "
        "`locked`, `blocked` by an overlapping lock or `future` (not ended yet), and the "
        "accounting years that can be shown with how many of their periods are locked. The "
        "accounting year starts on the practice's `financial_year_start_month`/`_day` and is "
        "named by the calendar year it starts in. Requires `period_lock.read`."
    ),
    responses=error_responses(403, 404),
)
async def get_period_calendar(
    org: Reader,
    service: ServiceDep,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
) -> ApiResponse[PeriodLockCalendar]:
    return ok(await service.calendar(org, year))


@router.put(
    "/period-locks/settings",
    summary="Set how the practice locks",
    description=(
        "Chooses the length of a lock (`granularity`: `weekly`, the default, `monthly`, "
        "`quarterly` or `annually`) and, optionally, the day the accounting year starts "
        "(`financial_year_start_month` 1-12 and `_day` 1-28; 1 January by default). This is the "
        "first-time set-up and can be changed later; locks that exist stay as they are. Returns "
        "the locks. Audited. Requires `period_lock.manage`."
    ),
    responses=error_responses(403, 404),
)
async def update_period_lock_settings(
    body: PeriodLockSettingsUpdate, org: Locker, service: ServiceDep, ctx: ContextDep
) -> ApiResponse[PeriodLockRead]:
    return ok(await service.update_settings(org, body, ctx), "Period lock settings saved")


@router.get(
    "/period-locks/history",
    summary="List the lock history",
    description=(
        "Every lock and reopening, newest first, with the date before and after, the reason "
        "and who did it. Cursor-paginated (`limit`, `cursor`, `meta.next_cursor`). The history "
        "is append-only. Requires `period_lock.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_period_lock_history(
    org: Reader, service: ServiceDep, params: PageParamsDep
) -> ApiResponse[list[PeriodLockEventRead]]:
    page = await service.history(org, params)
    return ok_page(page.items, page.next_cursor)


@router.post(
    "/period-locks",
    summary="Lock a period",
    description=(
        "Locks one `week`, `month`, `quarter` or `year` of the accounting `year` (`period` is the "
        "week 1-53, month 1-12 or quarter 1-4 counted from the start of that year) and writes a "
        "`locked` event. `period_type` must be the length the practice locks (422 "
        "`PERIOD_LOCK_WRONG_LENGTH`; see `PUT /period-locks/settings`), so a range that is not "
        "exactly one such period cannot be locked. The period must have ended (422 "
        "`PERIOD_LOCK_IN_FUTURE`) and must not overlap a period that is already locked (422 "
        "`PERIOD_LOCK_OVERLAPS`). Returns the locks. Audited. Requires `period_lock.manage`."
    ),
    responses=error_responses(403, 404),
)
async def lock_period(
    body: PeriodLockCreate, org: Locker, service: ServiceDep, ctx: ContextDep
) -> ApiResponse[PeriodLockRead]:
    return ok(await service.lock(org, body, ctx), "Period locked")


@router.post(
    "/period-locks/reopen",
    summary="Unlock a locked period",
    description=(
        "Unlocks the period `lock_id` (404 when it is not locked) and writes a `reopened` event. "
        "`reason` is required (422 `PERIOD_LOCK_REASON_REQUIRED`). Returns the locks. Audited. "
        "Requires `period_lock.reopen`."
    ),
    responses=error_responses(403, 404),
)
async def reopen_period(
    body: PeriodLockReopen, org: Reopener, service: ServiceDep, ctx: ContextDep
) -> ApiResponse[PeriodLockRead]:
    return ok(await service.reopen(org, body, ctx), "Period unlocked")
