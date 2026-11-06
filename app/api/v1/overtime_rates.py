"""Dated overtime rates of grades and staff members (APT81, phase A). HTTP only."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import require
from app.repositories.overtime import OvertimeRateFilters
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.overtime import (
    OvertimeLevel,
    OvertimeRateCreate,
    OvertimeRateEnd,
    OvertimeRateRead,
    OvertimeResolveRead,
)
from app.schemas.rate_rules import RuleStatus
from app.services.overtime_rates import OvertimeRateService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Rates"])

RateReader = Annotated[OrgContext, Depends(require("rate.read"))]
RateManager = Annotated[OrgContext, Depends(require("rate.manage"))]


def get_overtime_rate_service(session: SessionDep) -> OvertimeRateService:
    return OvertimeRateService(session)


OvertimeServiceDep = Annotated[OvertimeRateService, Depends(get_overtime_rate_service)]
_NOT_PRICED = " Time is not priced from overtime rates yet."


@router.get(
    "/overtime-rates",
    summary="List overtime rates",
    description=(
        "Dated overtime cost rates: grade rates first (by grade name), then staff rates (by "
        "name), newest period first. A rate is a `multiplier` of the standard cost rate "
        "(`multiplier_bp`, 15000 = 1.5x) or a `fixed` amount (`rate`, pence per hour). Filters: "
        "`level` (`grade` | `staff`), `user_id`, `grade_id`, `status` (`current`, `future` or "
        "`ended`, today in the practice time zone). Not paginated."
        + _NOT_PRICED
        + " Requires `rate.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_overtime_rates(
    org: RateReader,
    service: OvertimeServiceDep,
    level: Annotated[OvertimeLevel | None, Query(description="grade or staff")] = None,
    user_id: Annotated[uuid.UUID | None, Query(description="Rates of this staff member")] = None,
    grade_id: Annotated[uuid.UUID | None, Query(description="Rates of this grade")] = None,
    rate_status: Annotated[
        RuleStatus | None,
        Query(alias="status", description="current, future or ended, as of today"),
    ] = None,
) -> ApiResponse[list[OvertimeRateRead]]:
    views = await service.list_rates(
        org,
        OvertimeRateFilters(level=level, user_id=user_id, grade_id=grade_id, status=rate_status),
    )
    today = service.today(org)
    return ok([OvertimeRateRead.from_view(v, today=today, show_cost=True) for v in views])


@router.post(
    "/overtime-rates",
    status_code=status.HTTP_201_CREATED,
    summary="Add an overtime rate",
    description=(
        "Adds a dated overtime cost rate. `level` `grade` names a `grade_id`, `level` `staff` a "
        "`user_id` (never both). `mode` `multiplier` takes `multiplier_bp` (10000-100000, "
        "times the standard cost rate); `mode` `fixed` takes `rate` (pence per hour). The rate "
        "applies from `effective_from` to `effective_to` inclusive (open-ended when null). "
        "Validation errors are 422 on the field (an unknown grade or staff member included). "
        "409 `RATE_OVERLAP` when another rate of the same grade or staff member covers any day "
        "of the period (`details.conflicts` lists them); 409 `MEMBER_REMOVED` for a person who "
        "has left. A staff rate overrides the grade rate; they never stack. `rate` is null in "
        "the response without `rate.read`." + _NOT_PRICED + " Requires `rate.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_overtime_rate(
    body: OvertimeRateCreate, org: RateManager, service: OvertimeServiceDep, ctx: ContextDep
) -> ApiResponse[OvertimeRateRead]:
    view = await service.create(org, body, ctx)
    return ok(
        OvertimeRateRead.from_view(
            view, today=service.today(org), show_cost=service.can_see_costs(org)
        ),
        "Overtime rate added",
    )


@router.get(
    "/overtime-rates/resolve",
    summary="The overtime rate that applies",
    description=(
        "The one overtime rate of a staff member (`user_id`) on `date` (default today): their "
        "own staff rate, else their grade's rate, else none (`source` null, `rate` null). A "
        "staff rate overrides the grade rate; they never stack. An unknown staff member is "
        "404. Nothing is written." + _NOT_PRICED + " Requires `rate.read`."
    ),
    responses=error_responses(403, 404),
)
async def resolve_overtime_rate(
    org: RateReader,
    service: OvertimeServiceDep,
    user_id: Annotated[uuid.UUID, Query(description="The staff member")],
    on_date: Annotated[date | None, Query(alias="date", description="Default today")] = None,
) -> ApiResponse[OvertimeResolveRead]:
    return ok(await service.resolve(org, user_id, on_date))


# --- One rate (declared after the static paths: `{rate_id}` would swallow them) ----------


@router.patch(
    "/overtime-rates/{rate_id}",
    summary="End an overtime rate",
    description=(
        "Sets the rate's last day (`effective_to`, inclusive), which may be in the past or the "
        "future. 422 on `effective_to` when it is before the rate's first day; 409 "
        "`RATE_OVERLAP` when the new period meets another rate of the same grade or staff "
        "member. `rate` is null in the response without `rate.read`. Requires `rate.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def end_overtime_rate(
    rate_id: uuid.UUID,
    body: OvertimeRateEnd,
    org: RateManager,
    service: OvertimeServiceDep,
    ctx: ContextDep,
) -> ApiResponse[OvertimeRateRead]:
    view = await service.end(org, rate_id, body, ctx)
    return ok(
        OvertimeRateRead.from_view(
            view, today=service.today(org), show_cost=service.can_see_costs(org)
        ),
        "Overtime rate ended",
    )


@router.delete(
    "/overtime-rates/{rate_id}",
    summary="Delete an overtime rate",
    description=(
        "Deletes the rate. No time is priced from overtime rates yet, so no rate is in use. "
        "Unknown ids are 404. Requires `rate.manage`."
    ),
    responses=error_responses(403, 404),
)
async def delete_overtime_rate(
    rate_id: uuid.UUID, org: RateManager, service: OvertimeServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.delete(org, rate_id, ctx)
    return ok(message="Overtime rate deleted")
