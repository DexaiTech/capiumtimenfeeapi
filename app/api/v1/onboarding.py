"""Onboarding checklist: the practice's set-up progress. HTTP only."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import require
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.onboarding import OnboardingRead, OnboardingStepUpdate
from app.services.onboarding import OnboardingService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Onboarding"])

Reader = Annotated[OrgContext, Depends(require("organization.read"))]
Manager = Annotated[OrgContext, Depends(require("onboarding.manage"))]


def get_onboarding_service(session: SessionDep) -> OnboardingService:
    return OnboardingService(session)


ServiceDep = Annotated[OnboardingService, Depends(get_onboarding_service)]


@router.get(
    "/onboarding",
    summary="Get the onboarding checklist",
    description=(
        "The steps of the practice's set-up checklist, in the order to work through them, each "
        "with a `state` (`done`, `todo` or `skipped`), the `source` of that state (`auto`: "
        "counted from the practice's data; `manual`: a person marked it) and a deep `link` below "
        "the practice's URL. `summary` counts the steps. Every member of the practice may read "
        "it (`organization.read`); it shows counts and no records."
    ),
    responses=error_responses(403, 404),
)
async def get_onboarding(org: Reader, service: ServiceDep) -> ApiResponse[OnboardingRead]:
    return ok(await service.get(org))


@router.put(
    "/onboarding/steps/{step_key}",
    summary="Mark an onboarding step",
    description=(
        "`done` or `skipped` records a decision that wins over the data; `todo` takes it back, "
        "so a step the data can count is decided by the data again. Marking the state a step "
        "already has writes nothing. Returns the whole checklist. 404 for an unknown step. "
        "Audited as `onboarding.step_updated`. Requires `onboarding.manage`."
    ),
    responses=error_responses(403, 404),
)
async def set_onboarding_step(
    step_key: str,
    body: OnboardingStepUpdate,
    org: Manager,
    service: ServiceDep,
    ctx: ContextDep,
) -> ApiResponse[OnboardingRead]:
    return ok(await service.set_step(org, step_key, body, ctx), "Step updated")
