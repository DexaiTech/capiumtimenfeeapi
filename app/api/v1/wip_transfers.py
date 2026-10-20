"""WIP transfers: move WIP from one job of a client to another job of the same client. HTTP only;
the rules live in ``WipTransferService``."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.repositories.wip_ledger import TransferFilters
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.wip import TransferCreate, TransferRead, TransferReverse, TransferStatus
from app.services.policy import OrgContext
from app.services.wip_transfers import WipTransferService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["WIP"])


def get_transfer_service(session: SessionDep) -> WipTransferService:
    return WipTransferService(session)


TransferServiceDep = Annotated[WipTransferService, Depends(get_transfer_service)]
WipReader = Annotated[OrgContext, Depends(require("wip.read"))]
WipTransferer = Annotated[OrgContext, Depends(require("wip.transfer"))]


@router.post(
    "/wip/transfers",
    status_code=status.HTTP_201_CREATED,
    summary="Transfer WIP between jobs",
    description=(
        "Moves the value of the listed unbilled `items` (time entries, disbursements or "
        "expense lines of the source job, of one kind of WIP) or an `amount` from "
        "`from_job_id` to `to_job_id`. Both jobs must be the same client's (422 "
        "`CLIENT_MISMATCH`), different (422 `SAME_JOB`) and open (409 `JOB_NOT_OPEN`). A "
        "transfer writes two ledger movements, out of the source and into the target, and "
        "leaves the items as they are; items that are not unbilled WIP of the source job "
        "answer 409 `WIP_NOT_AVAILABLE`, items moved already 409 `ITEM_ALREADY_TRANSFERRED`, "
        "an amount above the WIP there is 409 `INSUFFICIENT_WIP`. A reason is required; the "
        "date is today by default and the WIP period lock applies to it. Audited. Requires "
        "`wip.transfer`."
    ),
    response_model=ApiResponse[TransferRead],
    responses=error_responses(403, 404, 409),
)
async def create_transfer(
    body: TransferCreate, org: WipTransferer, service: TransferServiceDep, ctx: ContextDep
) -> ApiResponse[TransferRead]:
    return ok(await service.create(org, body, ctx), "WIP transferred")


@router.get(
    "/wip/transfers",
    summary="List WIP transfers",
    description=(
        "Transfers newest first, cursor-paginated. Filters: status, client, job (as source or "
        "target) and the date range. Requires `wip.read`."
    ),
    response_model=ApiResponse[list[TransferRead]],
    responses=error_responses(403),
)
async def list_transfers(
    org: WipReader,
    service: TransferServiceDep,
    page: PageParamsDep,
    status_filter: Annotated[
        TransferStatus | None, Query(alias="status", description="`applied` or `reversed`")
    ] = None,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client's")] = None,
    job_id: Annotated[uuid.UUID | None, Query(description="Source or target job")] = None,
    date_from: Annotated[date | None, Query(description="Effective on or after")] = None,
    date_to: Annotated[date | None, Query(description="Effective on or before")] = None,
) -> ApiResponse[list[TransferRead]]:
    filters = TransferFilters(
        status=status_filter,
        client_id=client_id,
        job_id=job_id,
        date_from=date_from,
        date_to=date_to,
    )
    result = await service.list_transfers(org, filters, page)
    return ok_page(result.items, result.next_cursor)


@router.get(
    "/wip/transfers/{transfer_id}",
    summary="Get a WIP transfer",
    description="One transfer with the items it moved. Requires `wip.read`.",
    response_model=ApiResponse[TransferRead],
    responses=error_responses(403, 404),
)
async def get_transfer(
    transfer_id: uuid.UUID, org: WipReader, service: TransferServiceDep
) -> ApiResponse[TransferRead]:
    return ok(await service.get(org, transfer_id))


@router.post(
    "/wip/transfers/{transfer_id}/reverse",
    summary="Reverse a WIP transfer",
    description=(
        "Moves the value back by writing the opposite movements; the original rows stay. "
        "A reason is required. Not possible once either job has been billed since (409 "
        "`TRANSFER_SETTLED`) or when the transfer is reversed already (409 "
        "`TRANSFER_NOT_REVERSIBLE`). Audited. Requires `wip.transfer`."
    ),
    response_model=ApiResponse[TransferRead],
    responses=error_responses(403, 404, 409),
)
async def reverse_transfer(
    transfer_id: uuid.UUID,
    body: TransferReverse,
    org: WipTransferer,
    service: TransferServiceDep,
    ctx: ContextDep,
) -> ApiResponse[TransferRead]:
    return ok(await service.reverse(org, transfer_id, body, ctx), "Transfer reversed")
