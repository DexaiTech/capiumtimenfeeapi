"""Practice (organization) routes. HTTP only: every rule lives in ``OrganizationService``."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, status

from app.dependencies.auth import VerifiedPrincipalDep
from app.dependencies.common import ContextDep, SessionDep, forbid_when_closed
from app.dependencies.organization import require
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.organization import (
    OrganizationContextRead,
    OrganizationCreate,
    OrganizationDelete,
    OrganizationRead,
    OrganizationUpdate,
)
from app.services.organizations import OrganizationService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations", tags=["Organizations"])
me_router = APIRouter(prefix="/users/me", tags=["Organizations"])


def get_organization_service(session: SessionDep) -> OrganizationService:
    return OrganizationService(session)


OrganizationServiceDep = Annotated[OrganizationService, Depends(get_organization_service)]
NoCreationWhenClosed = Depends(
    forbid_when_closed(
        "ORGANIZATION_CREATION_DISABLED",
        "Practices cannot be created in this deployment.",
    )
)


@me_router.get(
    "/organizations",
    summary="List my practices",
    description=(
        "Every practice the caller is an active member of, with the caller's role and "
        "permissions in each, ordered by name. Not paginated: the set is small and bounded."
    ),
    responses=error_responses(403),
)
async def list_my_organizations(
    principal: VerifiedPrincipalDep, service: OrganizationServiceDep
) -> ApiResponse[list[OrganizationContextRead]]:
    found = await service.list_mine(principal.user)
    return ok([OrganizationContextRead.build(organization, role) for organization, role in found])


@router.post(
    "",
    dependencies=[NoCreationWhenClosed],
    status_code=status.HTTP_201_CREATED,
    summary="Create a practice",
    description=(
        "Creates the practice, copies the seven system roles (Owner, Admin, Partner, Manager, "
        "Staff, Billing, Viewer) with their permissions, seeds the defaults (UK VAT rates, "
        "activity codes, common services, number sequences) and makes the caller Owner. "
        "`currency` (default GBP) is fixed at creation. Without a slug one is derived from "
        "the name and made unique with a numeric suffix. A user may own at most 10 practices. "
        "403 `ORGANIZATION_CREATION_DISABLED` in a closed deployment."
    ),
    responses=error_responses(403, 409),
)
async def create_organization(
    body: OrganizationCreate,
    principal: VerifiedPrincipalDep,
    service: OrganizationServiceDep,
    ctx: ContextDep,
) -> ApiResponse[OrganizationContextRead]:
    organization, role = await service.create(principal.user, body, ctx)
    return ok(OrganizationContextRead.build(organization, role), "Practice created")


@router.get(
    "/by-slug/{slug}",
    summary="Resolve a practice by slug",
    description=(
        "Returns the practice with the caller's role and permissions. 404 when the slug "
        "is unknown or the caller is not an active member."
    ),
    responses=error_responses(403, 404),
)
async def get_organization_by_slug(
    slug: Annotated[str, Path(min_length=1, max_length=40)],
    principal: VerifiedPrincipalDep,
    service: OrganizationServiceDep,
) -> ApiResponse[OrganizationContextRead]:
    organization, role = await service.get_by_slug(principal.user, slug)
    return ok(OrganizationContextRead.build(organization, role))


@router.get(
    "/{organization_id}",
    summary="Get a practice",
    description=(
        "The practice (with its billing settings) and the caller's role and permissions in it. "
        "Requires `organization.read`."
    ),
    responses=error_responses(403, 404),
)
async def get_organization(
    org: Annotated[OrgContext, Depends(require("organization.read"))],
) -> ApiResponse[OrganizationContextRead]:
    return ok(OrganizationContextRead.build(org.organization, org.role))


@router.patch(
    "/{organization_id}",
    summary="Update practice settings",
    description=(
        "Changes any of the name, timezone, week start, working days, minutes per day and the "
        "billing settings: VAT registration and number, company number, payment terms, "
        "default charge rate (pence per hour), time increment (1, 5, 6, 10, 15, 30 or 60 "
        "minutes; 6 for new practices), whether timesheets need approval before billing, "
        "contact and address details, bank details and invoice footer; and the expense, WIP "
        "and rate settings: `fixed_fee_wip_mode` (`on_completion` or `by_budget_progress`), "
        "`wip_adjustment_approval_threshold`, `receipt_required_threshold`, "
        "`invoice_approval_required`, `invoice_approval_threshold` (pence, 0 to 100000000), "
        "`payment_reminders_enabled`, `payment_reminder_days_after_due` (1 to 365), "
        "`mileage_rate` (pence per mile, 0 to 1000), `expense_markup_bp` (basis points, 0 to "
        "100000) and `rate_mode` (`staff_first` or `grade_only`). Optional text fields are "
        "cleared with `null` or an empty string. The slug and the currency cannot be changed "
        "(422). Every change is audited with its old and new value. Requires "
        "`organization.update`."
    ),
    responses=error_responses(403, 404),
)
async def update_organization(
    body: OrganizationUpdate,
    org: Annotated[OrgContext, Depends(require("organization.update"))],
    service: OrganizationServiceDep,
    ctx: ContextDep,
) -> ApiResponse[OrganizationRead]:
    organization = await service.update(org, body, ctx)
    return ok(OrganizationRead.from_model(organization), "Practice settings updated")


@router.delete(
    "/{organization_id}",
    summary="Delete a practice",
    description=(
        "Soft-deletes the practice. `confirm_name` must equal the practice name. "
        "Requires `organization.delete`."
    ),
    responses=error_responses(403, 404),
)
async def delete_organization(
    body: OrganizationDelete,
    org: Annotated[OrgContext, Depends(require("organization.delete"))],
    service: OrganizationServiceDep,
    ctx: ContextDep,
) -> ApiResponse[None]:
    await service.delete(org, body, ctx)
    return ok(message="Practice deleted")
