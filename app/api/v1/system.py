"""Public information about this deployment. HTTP only."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies.common import SessionDep, SettingsDep
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.organization import SystemOrganization, SystemRead
from app.services.organizations import OrganizationService

router = APIRouter(prefix="/system", tags=["System"])


def get_organization_service(session: SessionDep) -> OrganizationService:
    return OrganizationService(session)


OrganizationServiceDep = Annotated[OrganizationService, Depends(get_organization_service)]


@router.get(
    "",
    summary="Deployment information",
    description=(
        "Public. `closed_deployment` mirrors the `CLOSED_DEPLOYMENT` setting. In a closed "
        "deployment `organization` is the one practice (name and slug, null until "
        "`python -m app.maintenance.bootstrap` has run), otherwise null. "
        "Covered by the per-address API rate limit like the other public routes."
    ),
    responses=error_responses(),
)
async def get_system(
    settings: SettingsDep, service: OrganizationServiceDep
) -> ApiResponse[SystemRead]:
    organization = await service.first_organization() if settings.closed_deployment else None
    return ok(
        SystemRead(
            closed_deployment=settings.closed_deployment,
            organization=(
                SystemOrganization(name=organization.name, slug=organization.slug)
                if organization
                else None
            ),
        )
    )
