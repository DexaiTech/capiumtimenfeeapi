"""Overtime of time entries: the approval list and decisions (APT81, phase C). HTTP only."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, PageParamsDep
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.overtime import OvertimeApprove, OvertimeRead, OvertimeReject, OvertimeStatus
from app.services.overtime import OvertimeService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Time"])


def get_overtime_service(session: SessionDep) -> OvertimeService:
    return OvertimeService(session)


OvertimeServiceDep = Annotated[OvertimeService, Depends(get_overtime_service)]
_COST = " `overtime_rate`, `overtime_cost` and `premium_cost` are null without `report.staff_cost`."
_DECIDE = (
    " Allowed for the person's approver (`timesheet.approve` and their manager) or anyone with "
    "`time.manage_all`; otherwise 403. Deciding on one's own overtime needs "
    "`organization.update` (403 `SELF_APPROVAL_FORBIDDEN`). 409 `INVALID_STATUS_TRANSITION` "
    "when it already has that status; 409 `PERIOD_LOCKED` when its day is in a locked period; "
    "404 for an unknown id."
)


@router.get(
    "/overtime",
    summary="List overtime",
    description=(
        "The overtime portions of time entries (worked out from each person's working hours "
        "while the practice has overtime on), oldest entry first, each with its entry, job and "
        "client. Filters: `user_id`, `status` (`pending` | `approved` | `rejected`), "
        "`date_from` and `date_to` (entry dates, inclusive; also accepted as `from` and `to`), "
        "`allocated` (true: on a client job; false: on the "
        "Internal client). Everyone's with `time.read_all` or `time.manage_all`; otherwise one's "
        "own and the people one manages (403 for anyone else's `user_id`). Cursor-paginated "
        "(`limit`, `cursor`; `meta.next_cursor`)." + _COST
    ),
    responses=error_responses(403, 404),
)
async def list_overtime(
    org: OrgContextDep,
    service: OvertimeServiceDep,
    page: PageParamsDep,
    user_id: Annotated[uuid.UUID | None, Query(description="Only this person's overtime")] = None,
    overtime_status: Annotated[
        OvertimeStatus | None,
        Query(alias="status", description="pending, approved or rejected"),
    ] = None,
    date_from: Annotated[date | None, Query(description="Entries on or after this day")] = None,
    date_to: Annotated[date | None, Query(description="Entries on or before this day")] = None,
    from_: Annotated[
        date | None, Query(alias="from", description="Same as `date_from` (either may be sent)")
    ] = None,
    to: Annotated[
        date | None, Query(alias="to", description="Same as `date_to` (either may be sent)")
    ] = None,
    allocated: Annotated[
        bool | None, Query(description="true: allocated (client jobs); false: Internal client")
    ] = None,
) -> ApiResponse[list[OvertimeRead]]:
    result = await service.list_overtime(
        org,
        user_id=user_id,
        status=overtime_status,
        date_from=date_from or from_,
        date_to=date_to or to,
        allocated=allocated,
        params=page,
    )
    return ok_page(result.items, result.next_cursor)


@router.post(
    "/overtime/{overtime_id}/approve",
    summary="Approve overtime",
    description=(
        "Marks the overtime `approved` (from `pending`, or from `rejected`, which puts its "
        "premium back), with an optional `note`. Audited `overtime.approved`." + _DECIDE + _COST
    ),
    responses=error_responses(403, 404, 409),
)
async def approve_overtime(
    overtime_id: uuid.UUID,
    org: OrgContextDep,
    service: OvertimeServiceDep,
    ctx: ContextDep,
    body: OvertimeApprove | None = None,
) -> ApiResponse[OvertimeRead]:
    note = body.note if body else None
    return ok(await service.decide(org, overtime_id, "approved", note, ctx), "Overtime approved")


@router.post(
    "/overtime/{overtime_id}/reject",
    summary="Reject overtime",
    description=(
        "Marks the overtime `rejected` with a required `note` (422 without one). Only the "
        "premium is removed: the hours stay, at their standard cost, and the Staff Cost report "
        "counts them as standard. Audited `overtime.rejected`." + _DECIDE + _COST
    ),
    responses=error_responses(403, 404, 409),
)
async def reject_overtime(
    overtime_id: uuid.UUID,
    body: OvertimeReject,
    org: OrgContextDep,
    service: OvertimeServiceDep,
    ctx: ContextDep,
) -> ApiResponse[OvertimeRead]:
    return ok(
        await service.decide(org, overtime_id, "rejected", body.note, ctx), "Overtime rejected"
    )
