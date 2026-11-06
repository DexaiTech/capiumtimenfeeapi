"""A staff member's dated working hours (APT81, phase A). HTTP only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import require
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.overtime import StaffHoursCreate, StaffHoursRead
from app.services.policy import OrgContext
from app.services.staff_hours import StaffHoursService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Staff"])

StaffReader = Annotated[OrgContext, Depends(require("member.read"))]
StaffEditor = Annotated[OrgContext, Depends(require("member.update"))]


def get_staff_hours_service(session: SessionDep) -> StaffHoursService:
    return StaffHoursService(session)


StaffHoursServiceDep = Annotated[StaffHoursService, Depends(get_staff_hours_service)]


@router.get(
    "/staff/{user_id}/hours",
    summary="List a staff member's working hours",
    description=(
        "The staff member's dated working hours: `basis` (`weekly` or `monthly`) and "
        "`standard_minutes` in that period, beyond which time is overtime. The current period "
        "comes first, then the rest newest first; `status` is `current`, `future` or `ended` "
        "(today in the practice time zone). An unknown staff member is 404. Requires "
        "`member.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_hours(
    user_id: uuid.UUID, org: StaffReader, service: StaffHoursServiceDep
) -> ApiResponse[list[StaffHoursRead]]:
    return ok(await service.list_hours(org, user_id))


@router.post(
    "/staff/{user_id}/hours",
    status_code=status.HTTP_201_CREATED,
    summary="Change a staff member's working hours",
    description=(
        "Adds open-ended working hours from `effective_from`: `basis` `weekly` (1-10080 "
        "minutes) or `monthly` (1-44640 minutes) and `standard_minutes`. The person's "
        "open-ended hours that started earlier end the day before (audited as "
        "`staff_hours.ended`). 409 `HOURS_OVERLAP` when other hours cover the period (hours "
        "starting the same day or later, or a period with an end date); `details.conflicts` "
        "lists them. Time is not priced from these hours yet. Requires `member.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def add_hours(
    user_id: uuid.UUID,
    body: StaffHoursCreate,
    org: StaffEditor,
    service: StaffHoursServiceDep,
    ctx: ContextDep,
) -> ApiResponse[StaffHoursRead]:
    return ok(await service.add(org, user_id, body, ctx), "Working hours changed")


@router.delete(
    "/staff/{user_id}/hours/{hours_id}",
    summary="Delete working hours",
    description=(
        "Deletes one period of the staff member's working hours; the other periods are left as "
        "they are. Unknown ids are 404. Requires `member.update`."
    ),
    responses=error_responses(403, 404),
)
async def delete_hours(
    user_id: uuid.UUID,
    hours_id: uuid.UUID,
    org: StaffEditor,
    service: StaffHoursServiceDep,
    ctx: ContextDep,
) -> ApiResponse[None]:
    await service.delete(org, user_id, hours_id, ctx)
    return ok(message="Working hours deleted")
