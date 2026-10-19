"""Disbursement routes. HTTP only: every rule lives in ``DisbursementService``."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, PageParamsDep
from app.repositories.disbursements import DisbursementFilters
from app.schemas.billing import (
    BillingStatus,
    DisbursementCreate,
    DisbursementRead,
    DisbursementUpdate,
)
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.services.disbursements import DisbursementService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Disbursements"])


def get_disbursement_service(session: SessionDep) -> DisbursementService:
    return DisbursementService(session)


DisbursementServiceDep = Annotated[DisbursementService, Depends(get_disbursement_service)]


@router.get(
    "/disbursements",
    summary="List disbursements",
    description=(
        "Disbursements newest first by date, cursor-paginated. With `wip.read` all of them; "
        "with only `disbursement.log` the caller's own. Filters: client, job, billing status "
        "(repeatable), who recorded it, date range (inclusive) and billable."
    ),
    responses=error_responses(403, 404),
)
async def list_disbursements(
    org: OrgContextDep,
    service: DisbursementServiceDep,
    page: PageParamsDep,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client's")] = None,
    job_id: Annotated[uuid.UUID | None, Query(description="Only this job's")] = None,
    billing_status: Annotated[
        list[BillingStatus] | None, Query(description="Billing status; repeat for several")
    ] = None,
    created_by: Annotated[uuid.UUID | None, Query(description="Recorded by this user")] = None,
    date_from: Annotated[date | None, Query(description="Incurred on or after")] = None,
    date_to: Annotated[date | None, Query(description="Incurred on or before")] = None,
    is_billable: Annotated[bool | None, Query(description="Billable or not")] = None,
) -> ApiResponse[list[DisbursementRead]]:
    filters = DisbursementFilters(
        client_id=client_id,
        job_id=job_id,
        billing_status=billing_status,
        created_by=created_by,
        date_from=date_from,
        date_to=date_to,
        is_billable=is_billable,
    )
    result = await service.list_disbursements(org, filters, page)
    return ok_page([DisbursementRead.from_model(d) for d in result.items], result.next_cursor)


@router.post(
    "/disbursements",
    status_code=status.HTTP_201_CREATED,
    summary="Record a disbursement",
    description=(
        "A cost paid on the client's behalf, to recharge. The job, when given, must be the "
        "client's; the VAT rate (charged on the recharge) must be a live rate, and null "
        "means the default resolution when billed. Inactive and archived clients answer "
        "409 `CLIENT_INACTIVE`. Requires `disbursement.log` or `disbursement.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_disbursement(
    body: DisbursementCreate, org: OrgContextDep, service: DisbursementServiceDep, ctx: ContextDep
) -> ApiResponse[DisbursementRead]:
    row = await service.create(org, body, ctx)
    return ok(DisbursementRead.from_model(row), "Disbursement recorded")


@router.get(
    "/disbursements/{disbursement_id}",
    summary="Get a disbursement",
    description="One disbursement. Requires `wip.read`, or having recorded it.",
    responses=error_responses(403, 404),
)
async def get_disbursement(
    disbursement_id: uuid.UUID, org: OrgContextDep, service: DisbursementServiceDep
) -> ApiResponse[DisbursementRead]:
    return ok(DisbursementRead.from_model(await service.get(org, disbursement_id)))


@router.patch(
    "/disbursements/{disbursement_id}",
    summary="Update a disbursement",
    description=(
        "Changes an unbilled disbursement (409 `DISBURSEMENT_LOCKED` once it is on a draft, "
        "billed or written off). Moving it to another client clears the job unless one is "
        "given. Own disbursements need `disbursement.log`, anyone's `disbursement.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_disbursement(
    disbursement_id: uuid.UUID,
    body: DisbursementUpdate,
    org: OrgContextDep,
    service: DisbursementServiceDep,
    ctx: ContextDep,
) -> ApiResponse[DisbursementRead]:
    row = await service.update(org, disbursement_id, body, ctx)
    return ok(DisbursementRead.from_model(row), "Disbursement updated")


@router.delete(
    "/disbursements/{disbursement_id}",
    summary="Delete a disbursement",
    description=(
        "Deletes an unbilled disbursement (409 `DISBURSEMENT_LOCKED` otherwise). Own "
        "disbursements need `disbursement.log`, anyone's `disbursement.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_disbursement(
    disbursement_id: uuid.UUID, org: OrgContextDep, service: DisbursementServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.delete(org, disbursement_id, ctx)
    return ok(message="Disbursement deleted")
