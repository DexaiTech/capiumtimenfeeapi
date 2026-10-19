"""Staff grades with their default rates and chargeable target. HTTP only."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.repositories.grades import GradeFilters
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.grades import GradeCreate, GradeRead, GradeUpdate
from app.schemas.settings import DeleteOutcome
from app.services.grades import GradeService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Grades"])

GradeReader = Annotated[OrgContext, Depends(require("member.read"))]
GradeManager = Annotated[OrgContext, Depends(require("rate.manage"))]


def get_grade_service(session: SessionDep) -> GradeService:
    return GradeService(session)


GradeServiceDep = Annotated[GradeService, Depends(get_grade_service)]

_COSTS = (
    " `default_cost_rate` is null for a caller without `rate.read` (it is also null when the "
    "grade sets none)."
)


@router.get(
    "/grades",
    summary="List grades",
    description=(
        "Grades of the practice, cursor-paginated. `q` searches name, code and description; "
        "`is_active` limits to active or inactive grades; `sort` = `sort_order` (default) | "
        "`name` | `code`, `order` = `asc` | `desc`. Each grade carries `staff_count` (staff who "
        "have not left) and `can_delete`." + _COSTS + " Requires `member.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_grades(
    org: GradeReader,
    service: GradeServiceDep,
    page: PageParamsDep,
    q: Annotated[
        str | None, Query(max_length=200, description="Search name, code and description")
    ] = None,
    is_active: Annotated[bool | None, Query(description="Only active or inactive")] = None,
    sort: Annotated[
        Literal["sort_order", "name", "code"], Query(description="Sort key")
    ] = "sort_order",
    order: Annotated[Literal["asc", "desc"], Query(description="Sort direction")] = "asc",
) -> ApiResponse[list[GradeRead]]:
    result = await service.list_grades(
        org,
        GradeFilters(q=q.strip() if q and q.strip() else None, is_active=is_active),
        sort=sort,
        descending=order == "desc",
        params=page,
    )
    return ok_page(result.items, result.next_cursor)


@router.post(
    "/grades",
    status_code=status.HTTP_201_CREATED,
    summary="Create a grade",
    description=(
        "`code` is upper-cased (1-12 letters, digits, `_`, `-`) and unique per practice (409 "
        "`GRADE_CODE_TAKEN`), as is `name`, ignoring case (409 `GRADE_NAME_TAKEN`). "
        "`default_cost_rate` and `default_charge_rate` are pence per hour; "
        "`default_chargeable_target_bp` is basis points (7500 = 75%); each may be null. They "
        "are the baseline of the grade's rates: a dated grade rate overrides them while in "
        "force, and staff without a rate of their own are priced from them. New staff "
        "without an explicit target start at the grade's target. The default rates are "
        "recorded in the rate history. Requires `rate.manage`." + _COSTS
    ),
    responses=error_responses(403, 404, 409),
)
async def create_grade(
    body: GradeCreate, org: GradeManager, service: GradeServiceDep, ctx: ContextDep
) -> ApiResponse[GradeRead]:
    return ok(await service.create(org, body, ctx), "Grade created")


@router.get(
    "/grades/{grade_id}",
    summary="Get a grade",
    description="One grade with its staff count." + _COSTS + " Requires `member.read`.",
    responses=error_responses(403, 404),
)
async def get_grade(
    grade_id: uuid.UUID, org: GradeReader, service: GradeServiceDep
) -> ApiResponse[GradeRead]:
    return ok(await service.get(org, grade_id))


@router.patch(
    "/grades/{grade_id}",
    summary="Update a grade",
    description=(
        "Changes any field; `description` and the three defaults accept null to clear them. "
        "Changing a default rate never rewrites recorded time (re-rate unbilled time to "
        "apply it) and is recorded in the rate history. 409 `GRADE_CODE_TAKEN` / "
        "`GRADE_NAME_TAKEN` for duplicates. Requires `rate.manage`." + _COSTS
    ),
    responses=error_responses(403, 404, 409),
)
async def update_grade(
    grade_id: uuid.UUID,
    body: GradeUpdate,
    org: GradeManager,
    service: GradeServiceDep,
    ctx: ContextDep,
) -> ApiResponse[GradeRead]:
    return ok(await service.update(org, grade_id, body, ctx), "Grade updated")


@router.post(
    "/grades/{grade_id}/deactivate",
    summary="Deactivate a grade",
    description=(
        "A deactivated grade cannot be given to staff; staff who hold it keep it. "
        "Requires `rate.manage`."
    ),
    responses=error_responses(403, 404),
)
async def deactivate_grade(
    grade_id: uuid.UUID, org: GradeManager, service: GradeServiceDep, ctx: ContextDep
) -> ApiResponse[GradeRead]:
    return ok(await service.set_active(org, grade_id, False, ctx), "Grade deactivated")


@router.post(
    "/grades/{grade_id}/activate",
    summary="Reactivate a grade",
    description="Makes a deactivated grade available again. Requires `rate.manage`.",
    responses=error_responses(403, 404),
)
async def activate_grade(
    grade_id: uuid.UUID, org: GradeManager, service: GradeServiceDep, ctx: ContextDep
) -> ApiResponse[GradeRead]:
    return ok(await service.set_active(org, grade_id, True, ctx), "Grade activated")


@router.delete(
    "/grades/{grade_id}",
    summary="Delete or deactivate a grade",
    description=(
        "Deletes an unused grade. A grade held by any staff member (whatever their status) "
        "or named by a rate is deactivated instead; `data.outcome` says which (`deleted` or "
        "`deactivated`). Requires `rate.manage`."
    ),
    responses=error_responses(403, 404),
)
async def delete_grade(
    grade_id: uuid.UUID, org: GradeManager, service: GradeServiceDep, ctx: ContextDep
) -> ApiResponse[DeleteOutcome]:
    outcome = await service.delete(org, grade_id, ctx)
    message = (
        "Grade deleted"
        if outcome.outcome == "deleted"
        else "Grade is in use, so it was deactivated"
    )
    return ok(outcome, message)
