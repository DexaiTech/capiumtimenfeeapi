"""The practice dashboard: headline figures for the signed-in person. HTTP only."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies.common import SessionDep
from app.dependencies.organization import OrgContextDep
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.dashboard import Dashboard
from app.services.dashboard import DashboardService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Dashboard"])


def get_dashboard_service(session: SessionDep) -> DashboardService:
    return DashboardService(session)


DashboardServiceDep = Annotated[DashboardService, Depends(get_dashboard_service)]


@router.get(
    "/dashboard",
    summary="Dashboard",
    description=(
        "For the signed-in member: this week's hours (chargeable / total) against capacity, "
        "the running timer, past weeks not yet submitted and open jobs due within 14 days "
        "that they manage or are assigned to (overdue ones included). `approvals_waiting` "
        "is shown with `timesheet.approve` or `report.financial`. With `report.financial` "
        "also `financial`: total WIP, WIP over 90 days, debtors, overdue debtors, net billed "
        "this and last month, and the top 5 clients by WIP and by overdue balance. Dates "
        "are in the practice time zone. Any member."
    ),
    responses=error_responses(404),
)
async def get_dashboard(org: OrgContextDep, service: DashboardServiceDep) -> ApiResponse[Dashboard]:
    return ok(await service.get(org))
