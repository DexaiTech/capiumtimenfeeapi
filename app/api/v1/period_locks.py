"""Period locks: lock a month, quarter or year, unlock it, history. HTTP only."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.period_locks import (
    PeriodLockCreate,
    PeriodLockEventRead,
    PeriodLockRead,
    PeriodLockReopen,
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
        "The practice's locked months, quarters and years (`periods`, oldest first). Time, "
        "expenses and WIP dated inside any of them cannot be created, edited or deleted (409 "
        "`PERIOD_LOCKED`). `last_event` is the latest lock or unlock. Requires `period_lock.read`."
    ),
    responses=error_responses(403, 404),
)
async def get_period_lock(org: Reader, service: ServiceDep) -> ApiResponse[PeriodLockRead]:
    return ok(await service.get(org))


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
        "Locks one calendar `month`, `quarter` or `year` (`period` is the month 1-12 or quarter "
        "1-4) and writes a `locked` event. The period must have ended (422 "
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
