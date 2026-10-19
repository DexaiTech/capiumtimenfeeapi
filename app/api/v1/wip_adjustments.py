"""WIP adjustments: write a job's or client's WIP on or off, approve the large ones, reverse them.
HTTP only; the rules live in ``WipAdjustmentService``."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.repositories.wip_ledger import AdjustmentFilters
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.wip import (
    AdjustmentApprove,
    AdjustmentCounts,
    AdjustmentCreate,
    AdjustmentKind,
    AdjustmentRead,
    AdjustmentReject,
    AdjustmentReverse,
    AdjustmentStatus,
)
from app.services.policy import OrgContext
from app.services.wip_adjustments import WipAdjustmentService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["WIP"])


def get_adjustment_service(session: SessionDep) -> WipAdjustmentService:
    return WipAdjustmentService(session)


AdjustmentServiceDep = Annotated[WipAdjustmentService, Depends(get_adjustment_service)]
WipReader = Annotated[OrgContext, Depends(require("wip.read"))]
WipAdjuster = Annotated[OrgContext, Depends(require("wip.adjust"))]
WipApprover = Annotated[OrgContext, Depends(require("wip.approve"))]


@router.post(
    "/wip/adjustments",
    status_code=status.HTTP_201_CREATED,
    summary="Write WIP on or off",
    description=(
        "Adds to (`write_on`) or removes from (`write_off`) the work in progress of a client "
        "or of one of its jobs by an amount, with a reason (required) and an effective date "
        "(default today, never in the future). `component` says which WIP it adjusts: "
        "default `fixed_fee` on a fixed-fee job, else `time`. An amount within the "
        "practice's `wip_adjustment_approval_threshold` takes effect at once (`approved`); a "
        "larger one is `pending` until somebody with `wip.approve` approves it (the amount "
        "is not part of the WIP before). A write-off larger than the WIP there is answers "
        "409 `INSUFFICIENT_WIP`. The period lock of the WIP area applies to the effective "
        "date (409 `PERIOD_LOCKED`). The adjustment counts in the job's WIP until the job is "
        "next billed. Audited. Requires `wip.adjust`."
    ),
    response_model=ApiResponse[AdjustmentRead],
    responses=error_responses(403, 404, 409),
)
async def create_adjustment(
    body: AdjustmentCreate, org: WipAdjuster, service: AdjustmentServiceDep, ctx: ContextDep
) -> ApiResponse[AdjustmentRead]:
    created = await service.create(org, body, ctx)
    message = "WIP adjusted" if created.status == "approved" else "Adjustment sent for approval"
    return ok(created, message)


@router.get(
    "/wip/adjustments",
    summary="List WIP adjustments",
    description=(
        "Adjustments newest first, cursor-paginated. Filters: status (repeat the parameter), "
        "kind, client, job and the effective-date range. Each says whether it is `in_force`: "
        "approved, not reversed and not yet billed. Requires `wip.read`."
    ),
    response_model=ApiResponse[list[AdjustmentRead]],
    responses=error_responses(403),
)
async def list_adjustments(
    org: WipReader,
    service: AdjustmentServiceDep,
    page: PageParamsDep,
    status_filter: Annotated[
        list[AdjustmentStatus] | None, Query(alias="status", description="Only these statuses")
    ] = None,
    kind: Annotated[
        AdjustmentKind | None, Query(description="Only write-ons or write-offs")
    ] = None,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client's")] = None,
    job_id: Annotated[uuid.UUID | None, Query(description="Only this job's")] = None,
    date_from: Annotated[date | None, Query(description="Effective on or after")] = None,
    date_to: Annotated[date | None, Query(description="Effective on or before")] = None,
) -> ApiResponse[list[AdjustmentRead]]:
    filters = AdjustmentFilters(
        statuses=tuple(status_filter or ()),
        kind=kind,
        client_id=client_id,
        job_id=job_id,
        date_from=date_from,
        date_to=date_to,
    )
    result = await service.list(org, filters, page)
    return ok_page(result.items, result.next_cursor)


@router.get(
    "/wip/adjustments/counts",
    summary="Adjustments by status",
    description=(
        "How many adjustments are pending, approved, rejected and reversed, and the total "
        "amount waiting for approval. Requires `wip.read`."
    ),
    response_model=ApiResponse[AdjustmentCounts],
    responses=error_responses(403),
)
async def adjustment_counts(
    org: WipReader, service: AdjustmentServiceDep
) -> ApiResponse[AdjustmentCounts]:
    return ok(await service.counts(org))


@router.get(
    "/wip/adjustments/{adjustment_id}",
    summary="Get a WIP adjustment",
    description="One adjustment with its approval trail. Requires `wip.read`.",
    response_model=ApiResponse[AdjustmentRead],
    responses=error_responses(403, 404),
)
async def get_adjustment(
    adjustment_id: uuid.UUID, org: WipReader, service: AdjustmentServiceDep
) -> ApiResponse[AdjustmentRead]:
    return ok(await service.get(org, adjustment_id))


@router.post(
    "/wip/adjustments/{adjustment_id}/approve",
    summary="Approve a WIP adjustment",
    description=(
        "Approves a `pending` adjustment: it takes effect (a ledger movement is written) "
        "unless the period lock or the WIP available now forbid it (409). Nobody approves "
        "their own request unless they hold `organization.update` (403 "
        "`SELF_APPROVAL_FORBIDDEN`). A decided adjustment answers 409 "
        "`ADJUSTMENT_NOT_PENDING`. Audited. Requires `wip.approve`."
    ),
    response_model=ApiResponse[AdjustmentRead],
    responses=error_responses(403, 404, 409),
)
async def approve_adjustment(
    adjustment_id: uuid.UUID,
    org: WipApprover,
    service: AdjustmentServiceDep,
    ctx: ContextDep,
    body: AdjustmentApprove | None = None,
) -> ApiResponse[AdjustmentRead]:
    approved = await service.approve(org, adjustment_id, body or AdjustmentApprove(), ctx)
    return ok(approved, "Adjustment approved")


@router.post(
    "/wip/adjustments/{adjustment_id}/reject",
    summary="Reject a WIP adjustment",
    description=(
        "Rejects a `pending` adjustment with a reason (required); nothing is written to the "
        "ledger. Audited. Requires `wip.approve`."
    ),
    response_model=ApiResponse[AdjustmentRead],
    responses=error_responses(403, 404, 409),
)
async def reject_adjustment(
    adjustment_id: uuid.UUID,
    body: AdjustmentReject,
    org: WipApprover,
    service: AdjustmentServiceDep,
    ctx: ContextDep,
) -> ApiResponse[AdjustmentRead]:
    return ok(await service.reject(org, adjustment_id, body, ctx), "Adjustment rejected")


@router.post(
    "/wip/adjustments/{adjustment_id}/reverse",
    summary="Reverse a WIP adjustment",
    description=(
        "Cancels an `approved` adjustment with a reason (required) by writing the opposite "
        "movement to the ledger; the original stays. Not possible once the job has been billed "
        "since (409 `ADJUSTMENT_SETTLED`) or when the adjustment is not approved (409 "
        "`ADJUSTMENT_NOT_REVERSIBLE`). Period locked today answers 409 `PERIOD_LOCKED`. "
        "Audited. Requires `wip.adjust`."
    ),
    response_model=ApiResponse[AdjustmentRead],
    responses=error_responses(403, 404, 409),
)
async def reverse_adjustment(
    adjustment_id: uuid.UUID,
    body: AdjustmentReverse,
    org: WipAdjuster,
    service: AdjustmentServiceDep,
    ctx: ContextDep,
) -> ApiResponse[AdjustmentRead]:
    return ok(await service.reverse(org, adjustment_id, body, ctx), "Adjustment reversed")
