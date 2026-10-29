"""Client segments and the staff who look after them. HTTP only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, require
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.segments import (
    SegmentCreate,
    SegmentRead,
    SegmentUpdate,
    StaffSegmentsRead,
    StaffSegmentsWrite,
)
from app.schemas.settings import DeleteOutcome
from app.services.policy import OrgContext
from app.services.segments import SegmentService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Segments"])

SegmentManager = Annotated[OrgContext, Depends(require("settings.manage"))]
StaffReader = Annotated[OrgContext, Depends(require("member.read"))]
StaffEditor = Annotated[OrgContext, Depends(require("member.update"))]


def get_segment_service(session: SessionDep) -> SegmentService:
    return SegmentService(session)


SegmentServiceDep = Annotated[SegmentService, Depends(get_segment_service)]


@router.get(
    "/segments",
    summary="List segments",
    description=(
        "The practice's client segments, in `sort_order` then name. Bounded list, not "
        "paginated. `is_active` limits to active or inactive segments; `q` searches the name. "
        "Each carries `client_count` (live clients with the segment, matched ignoring case), "
        "`staff_count` and `can_delete`. Any member."
    ),
    responses=error_responses(404),
)
async def list_segments(
    org: OrgContextDep,
    service: SegmentServiceDep,
    is_active: Annotated[bool | None, Query(description="Only active or inactive")] = None,
    q: Annotated[str | None, Query(max_length=100, description="Search the name")] = None,
) -> ApiResponse[list[SegmentRead]]:
    return ok(await service.list_segments(org, is_active=is_active, q=q.strip() if q else None))


@router.post(
    "/segments",
    status_code=status.HTTP_201_CREATED,
    summary="Create a segment",
    description=(
        "`name` is up to 40 characters and unique per practice, ignoring case (409 "
        "`SEGMENT_NAME_TAKEN`). Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_segment(
    body: SegmentCreate, org: SegmentManager, service: SegmentServiceDep, ctx: ContextDep
) -> ApiResponse[SegmentRead]:
    return ok(await service.create(org, body, ctx), "Segment created")


@router.get(
    "/segments/{segment_id}",
    summary="Get a segment",
    description="One segment with its client and staff counts. Any member.",
    responses=error_responses(404),
)
async def get_segment(
    segment_id: uuid.UUID, org: OrgContextDep, service: SegmentServiceDep
) -> ApiResponse[SegmentRead]:
    return ok(await service.get(org, segment_id))


@router.patch(
    "/segments/{segment_id}",
    summary="Update a segment",
    description=(
        "Changes the name, description, order or active state. Renaming also renames the "
        "segment on the practice's live clients. 409 `SEGMENT_NAME_TAKEN` for a duplicate. "
        "Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_segment(
    segment_id: uuid.UUID,
    body: SegmentUpdate,
    org: SegmentManager,
    service: SegmentServiceDep,
    ctx: ContextDep,
) -> ApiResponse[SegmentRead]:
    return ok(await service.update(org, segment_id, body, ctx), "Segment updated")


@router.post(
    "/segments/{segment_id}/deactivate",
    summary="Deactivate a segment",
    description=(
        "A deactivated segment is not offered for new clients or staff; clients and staff "
        "that have it keep it. Requires `settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def deactivate_segment(
    segment_id: uuid.UUID, org: SegmentManager, service: SegmentServiceDep, ctx: ContextDep
) -> ApiResponse[SegmentRead]:
    return ok(await service.set_active(org, segment_id, False, ctx), "Segment deactivated")


@router.post(
    "/segments/{segment_id}/activate",
    summary="Reactivate a segment",
    description="Makes a deactivated segment available again. Requires `settings.manage`.",
    responses=error_responses(403, 404),
)
async def activate_segment(
    segment_id: uuid.UUID, org: SegmentManager, service: SegmentServiceDep, ctx: ContextDep
) -> ApiResponse[SegmentRead]:
    return ok(await service.set_active(org, segment_id, True, ctx), "Segment activated")


@router.delete(
    "/segments/{segment_id}",
    summary="Delete or deactivate a segment",
    description=(
        "Deletes an unused segment (staff links go with it). A segment that live clients "
        "have is deactivated instead; `data.outcome` says which (`deleted` or "
        "`deactivated`). Requires `settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def delete_segment(
    segment_id: uuid.UUID, org: SegmentManager, service: SegmentServiceDep, ctx: ContextDep
) -> ApiResponse[DeleteOutcome]:
    outcome = await service.delete(org, segment_id, ctx)
    message = (
        "Segment deleted"
        if outcome.outcome == "deleted"
        else "Segment is in use, so it was deactivated"
    )
    return ok(outcome, message)


@router.get(
    "/staff/{staff_id}/segments",
    summary="List the segments of a staff member",
    description=(
        "The segments the staff member looks after, and `client_count`: the live clients in "
        "any of them (each counted once). The clients themselves are listed with "
        "`GET /clients?segment=...`. Requires `member.read`."
    ),
    responses=error_responses(403, 404),
)
async def get_staff_segments(
    staff_id: uuid.UUID, org: StaffReader, service: SegmentServiceDep
) -> ApiResponse[StaffSegmentsRead]:
    return ok(await service.staff_segments(org, staff_id))


@router.put(
    "/staff/{staff_id}/segments",
    summary="Set the segments of a staff member",
    description=(
        "Replaces the segments the staff member looks after with `segment_ids`. Only active "
        "segments can be added (422); ones already held stay even when deactivated. Audited "
        "as `staff.segments_updated`. Requires `member.update`."
    ),
    responses=error_responses(403, 404),
)
async def set_staff_segments(
    staff_id: uuid.UUID,
    body: StaffSegmentsWrite,
    org: StaffEditor,
    service: SegmentServiceDep,
    ctx: ContextDep,
) -> ApiResponse[StaffSegmentsRead]:
    return ok(
        await service.set_staff_segments(org, staff_id, body.segment_ids, ctx), "Segments saved"
    )
