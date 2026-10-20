"""Estimate routes. HTTP only: every rule lives in ``EstimateService``."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.repositories.estimates import DISPLAY_STATUSES, EstimateFilters
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.estimates import (
    EstimateConverted,
    EstimateCreate,
    EstimateListSummary,
    EstimateRead,
    EstimateSend,
    EstimateSummaryRead,
    EstimateUpdate,
)
from app.services.estimates import EstimateService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Estimates"])


def get_estimate_service(session: SessionDep) -> EstimateService:
    return EstimateService(session)


EstimateServiceDep = Annotated[EstimateService, Depends(get_estimate_service)]
Reader = Annotated[OrgContext, Depends(require("estimate.read"))]
Manager = Annotated[OrgContext, Depends(require("estimate.manage"))]
Converter = Annotated[OrgContext, Depends(require("estimate.manage", "invoice.create"))]


def _filters(
    client_id: uuid.UUID | None, statuses: list[str] | None, q: str | None
) -> EstimateFilters:
    return EstimateFilters(
        client_id=client_id,
        statuses=[s for s in statuses if s in DISPLAY_STATUSES] if statuses else None,
        q=q.strip() if q else None,
    )


@router.get(
    "/estimates",
    summary="List estimates",
    description=(
        "Newest first, cursor-paginated. Filters: client, `status` (repeat it: `draft`, `sent`, "
        "`accepted`, `declined`, `expired`) and `q` (number, reference or client). `expired` "
        "is a sent estimate past its expiry date. Requires `estimate.read`."
    ),
    responses=error_responses(403),
)
async def list_estimates(
    org: Reader,
    service: EstimateServiceDep,
    page: PageParamsDep,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client's")] = None,
    status_: Annotated[list[str] | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(max_length=100, description="Number, reference, client")] = None,
) -> ApiResponse[list[EstimateSummaryRead]]:
    items, next_cursor = await service.list_estimates(org, _filters(client_id, status_, q), page)
    return ok_page(items, next_cursor)


@router.get(
    "/estimates/summary",
    summary="Estimate counts per status",
    description="For the status tabs: the client and search filters apply, the status does not.",
    responses=error_responses(403),
)
async def estimates_summary(
    org: Reader,
    service: EstimateServiceDep,
    client_id: Annotated[uuid.UUID | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> ApiResponse[EstimateListSummary]:
    return ok(await service.list_summary(org, _filters(client_id, None, q)))


@router.post(
    "/estimates",
    status_code=status.HTTP_201_CREATED,
    summary="Write an estimate",
    description=(
        "Creates a draft. Each line is priced as an invoice line is (net = round(quantity x "
        "unit price); VAT from the chosen rate, else the service's, the client's, the "
        "practice's). It has no number until it is sent. Requires `estimate.manage`."
    ),
    responses=error_responses(403, 404),
)
async def create_estimate(
    body: EstimateCreate, org: Manager, service: EstimateServiceDep, ctx: ContextDep
) -> ApiResponse[EstimateRead]:
    return ok(await service.create(org, body, ctx), "Estimate created")


@router.get(
    "/estimates/{estimate_id}",
    summary="Get an estimate",
    responses=error_responses(403, 404),
)
async def get_estimate(
    estimate_id: uuid.UUID, org: Reader, service: EstimateServiceDep
) -> ApiResponse[EstimateRead]:
    return ok(await service.get(org, estimate_id))


@router.patch(
    "/estimates/{estimate_id}",
    summary="Change a draft estimate",
    description=(
        "Only a draft (409 `ESTIMATE_NOT_DRAFT`). `lines`, when sent, replaces every line. "
        "Requires `estimate.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_estimate(
    estimate_id: uuid.UUID,
    body: EstimateUpdate,
    org: Manager,
    service: EstimateServiceDep,
    ctx: ContextDep,
) -> ApiResponse[EstimateRead]:
    return ok(await service.update(org, estimate_id, body, ctx), "Estimate updated")


@router.delete(
    "/estimates/{estimate_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a draft estimate",
    description="Only a draft: a numbered estimate stays on record. Requires `estimate.manage`.",
    responses=error_responses(403, 404, 409),
)
async def delete_estimate(
    estimate_id: uuid.UUID, org: Manager, service: EstimateServiceDep, ctx: ContextDep
) -> Response:
    await service.delete(org, estimate_id, ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/estimates/{estimate_id}/send",
    summary="Send an estimate",
    description=(
        "Numbers a draft from the `estimate` sequence and marks it `sent` (409 "
        "`ESTIMATE_EMPTY` without lines). Email delivery is not wired yet. Requires "
        "`estimate.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def send_estimate(
    estimate_id: uuid.UUID,
    org: Manager,
    service: EstimateServiceDep,
    ctx: ContextDep,
    body: EstimateSend | None = None,
) -> ApiResponse[EstimateRead]:
    return ok(await service.send(org, estimate_id, body or EstimateSend(), ctx), "Estimate sent")


@router.post(
    "/estimates/{estimate_id}/accept",
    summary="Record that the client accepted",
    description="Only a sent estimate (409 `ESTIMATE_NOT_SENT`). Requires `estimate.manage`.",
    responses=error_responses(403, 404, 409),
)
async def accept_estimate(
    estimate_id: uuid.UUID, org: Manager, service: EstimateServiceDep, ctx: ContextDep
) -> ApiResponse[EstimateRead]:
    return ok(await service.respond(org, estimate_id, True, ctx), "Estimate accepted")


@router.post(
    "/estimates/{estimate_id}/decline",
    summary="Record that the client declined",
    description="Only a sent estimate (409 `ESTIMATE_NOT_SENT`). Requires `estimate.manage`.",
    responses=error_responses(403, 404, 409),
)
async def decline_estimate(
    estimate_id: uuid.UUID, org: Manager, service: EstimateServiceDep, ctx: ContextDep
) -> ApiResponse[EstimateRead]:
    return ok(await service.respond(org, estimate_id, False, ctx), "Estimate declined")


@router.post(
    "/estimates/{estimate_id}/convert",
    summary="Convert an estimate to a draft invoice",
    description=(
        "Copies the lines of a sent or accepted estimate into a new draft invoice (409 "
        "`ESTIMATE_NOT_CONVERTIBLE`, `ESTIMATE_ALREADY_CONVERTED`). Requires "
        "`estimate.manage` and `invoice.create`."
    ),
    responses=error_responses(403, 404, 409),
)
async def convert_estimate(
    estimate_id: uuid.UUID, org: Converter, service: EstimateServiceDep, ctx: ContextDep
) -> ApiResponse[EstimateConverted]:
    return ok(await service.convert(org, estimate_id, ctx), "Invoice drafted")
