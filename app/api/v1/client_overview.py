"""The Client 360 overview. HTTP only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.dependencies.common import SessionDep
from app.dependencies.organization import require
from app.schemas.client_overview import ClientOverview
from app.schemas.common import ApiResponse, error_responses, ok
from app.services.client_overview import ClientOverviewService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Clients"])

ClientReader = Annotated[OrgContext, Depends(require("client.read"))]
ClientIdPath = Annotated[uuid.UUID, Path(description="The client's id")]


def get_overview_service(session: SessionDep) -> ClientOverviewService:
    return ClientOverviewService(session)


OverviewServiceDep = Annotated[ClientOverviewService, Depends(get_overview_service)]


@router.get(
    "/clients/{client_id}/overview",
    summary="Get a client's 360 overview",
    description=(
        "The figures behind the Client 360 tabs in one call: `jobs` (by status), `time` "
        "(last 30 days and year to date, billable or not), `expenses`, `wip` (time, fixed "
        "fee, expense, total and ageing: the figures of the WIP screens), `invoices` (by "
        "status, billed year to date, outstanding, overdue), `payments`, the last 10 audit "
        "events about the client and `tab_counts` for the tab badges. A section the caller may "
        "not see is `null` (`job.read`, `time.read_all` or `time.log`, `wip.read` (or "
        "`disbursement.log`) for expenses, `invoice.read`, `payment.read`): never a 403 for "
        "the whole call. Money is pence; the year starts on 1 January in the practice time "
        "zone. 404 for another practice's client. Requires `client.read`."
    ),
    responses=error_responses(403, 404),
)
async def get_client_overview(
    client_id: ClientIdPath, org: ClientReader, service: OverviewServiceDep
) -> ApiResponse[ClientOverview]:
    return ok(await service.get(org, client_id))
