"""Work in progress routes: the WIP ledger and write-offs. HTTP only."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.repositories.wip import WipSummaryFilters, WriteOffFilters
from app.schemas.billing import ClientWipRead, WipClientSummary, WriteOffCreate, WriteOffRead
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.services.policy import OrgContext
from app.services.wip import WipService
from app.services.wip_views import WipViewService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["WIP"])


def get_wip_service(session: SessionDep) -> WipService:
    return WipService(session)


def get_view_service(session: SessionDep) -> WipViewService:
    return WipViewService(session)


WipServiceDep = Annotated[WipService, Depends(get_wip_service)]
ViewServiceDep = Annotated[WipViewService, Depends(get_view_service)]
WipReader = Annotated[OrgContext, Depends(require("wip.read"))]
WipWriter = Annotated[OrgContext, Depends(require("wip.write_off"))]


@router.get(
    "/wip",
    summary="WIP by client",
    description=(
        "One row per client with unbilled work in progress, ordered by client name and "
        "cursor-paginated: minutes and hours, time value (all unbilled billable time, "
        "fixed-fee jobs included), disbursements, total, the oldest item date and the value "
        "in age buckets (0-30, 31-60, 61-90, 91+ days by item date, as at today). WIP is "
        "billable unbilled time (on approved timesheets when the practice requires approval) "
        "and billable unbilled disbursements; `expense_value` adds the approved expense lines "
        "and `composition` splits the total into Time, Fixed fee and Expense WIP. Filters: "
        "client, the client's partner or manager, job. Requires `wip.read`. For other "
        "groupings and a day in the past see `GET /wip/summary`."
    ),
    responses=error_responses(403, 404),
)
async def wip_summary(
    org: WipReader,
    service: ViewServiceDep,
    page: PageParamsDep,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client")] = None,
    partner_user_id: Annotated[uuid.UUID | None, Query(description="Client partner")] = None,
    manager_user_id: Annotated[uuid.UUID | None, Query(description="Client manager")] = None,
    job_id: Annotated[uuid.UUID | None, Query(description="Only this job's WIP")] = None,
) -> ApiResponse[list[WipClientSummary]]:
    filters = WipSummaryFilters(
        client_id=client_id,
        partner_user_id=partner_user_id,
        manager_user_id=manager_user_id,
        job_id=job_id,
    )
    result = await service.legacy_summary(org, filters, page)
    return ok_page(result.items, result.next_cursor)


@router.get(
    "/clients/{client_id}/wip",
    summary="A client's WIP items",
    description=(
        "Every WIP item of the client in date order (bounded: not paginated): time entries "
        "with person, job, minutes, rate and value, disbursements, approved expense lines, "
        "the fixed-fee WIP of its jobs and the adjustments and transfers in force, plus "
        "totals and the `composition` into Time, Fixed fee and Expense WIP. Each item says "
        "its `component`, `age_days` and whether billing may take it (`selectable`). "
        "Requires `wip.read`."
    ),
    responses=error_responses(403, 404),
)
async def client_wip(
    client_id: uuid.UUID,
    org: WipReader,
    service: ViewServiceDep,
    job_id: Annotated[uuid.UUID | None, Query(description="Only this job's items")] = None,
) -> ApiResponse[ClientWipRead]:
    return ok(await service.client_wip(org, client_id, job_id))


@router.post(
    "/wip/write-offs",
    status_code=status.HTTP_201_CREATED,
    summary="Write off WIP",
    description=(
        "Takes WIP out of the ledger without an invoice: either the listed items or all WIP "
        "of `job_id`, with a reason and date (default today). Each item gets an allocation "
        "with billed value 0 and becomes `written_off`. Items that are not the client's "
        "answer 422 `CLIENT_MISMATCH`; items that are not unbilled WIP 409 "
        "`WIP_NOT_AVAILABLE`. Requires `wip.write_off`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_write_off(
    body: WriteOffCreate, org: WipWriter, service: WipServiceDep, ctx: ContextDep
) -> ApiResponse[WriteOffRead]:
    return ok(await service.create_write_off(org, body, ctx), "WIP written off")


@router.get(
    "/wip/write-offs",
    summary="List write-offs",
    description=(
        "WIP write-offs newest first by date, cursor-paginated; filters: client, job, date "
        "range. Requires `wip.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_write_offs(
    org: WipReader,
    service: WipServiceDep,
    page: PageParamsDep,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client's")] = None,
    job_id: Annotated[uuid.UUID | None, Query(description="Only this job's")] = None,
    date_from: Annotated[date | None, Query(description="Written off on or after")] = None,
    date_to: Annotated[date | None, Query(description="Written off on or before")] = None,
) -> ApiResponse[list[WriteOffRead]]:
    filters = WriteOffFilters(
        client_id=client_id, job_id=job_id, date_from=date_from, date_to=date_to
    )
    result, clients, counts = await service.list_write_offs(org, filters, page)
    items = [
        WriteOffRead.from_model(w, client=clients.get(w.client_id), item_count=counts.get(w.id, 0))
        for w in result.items
    ]
    return ok_page(items, result.next_cursor)


@router.get(
    "/wip/write-offs/{write_off_id}",
    summary="Get a write-off",
    description="One write-off with its items. Requires `wip.read`.",
    responses=error_responses(403, 404),
)
async def get_write_off(
    write_off_id: uuid.UUID, org: WipReader, service: WipServiceDep
) -> ApiResponse[WriteOffRead]:
    return ok(await service.get_write_off(org, write_off_id))


@router.delete(
    "/wip/write-offs/{write_off_id}",
    summary="Reverse a write-off",
    description=(
        "Deletes the write-off and returns its items to `unbilled`, as long as none of them "
        "has moved on since (409 `WRITE_OFF_BILLED`). Requires `wip.write_off`."
    ),
    responses=error_responses(403, 404, 409),
)
async def reverse_write_off(
    write_off_id: uuid.UUID, org: WipWriter, service: WipServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.reverse_write_off(org, write_off_id, ctx)
    return ok(message="Write-off reversed")
