"""The practice's branches (ticket APT74). HTTP only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, require
from app.schemas.branches import BranchCreate, BranchRead, BranchUpdate
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.settings import DeleteOutcome
from app.services.branches import BranchService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Branches"])

BranchManager = Annotated[OrgContext, Depends(require("settings.manage"))]


def get_branch_service(session: SessionDep) -> BranchService:
    return BranchService(session)


BranchServiceDep = Annotated[BranchService, Depends(get_branch_service)]


@router.get(
    "/branches",
    summary="List branches",
    description=(
        "The practice's branches, by name. Bounded list, not paginated. `is_active` limits to "
        "active or inactive branches; `q` searches the name. Each carries `client_count` (live "
        "clients in the branch; null without `client.read`) and `can_delete`. Any member."
    ),
    responses=error_responses(404),
)
async def list_branches(
    org: OrgContextDep,
    service: BranchServiceDep,
    is_active: Annotated[bool | None, Query(description="Only active or inactive")] = None,
    q: Annotated[str | None, Query(max_length=100, description="Search the name")] = None,
) -> ApiResponse[list[BranchRead]]:
    return ok(await service.list_branches(org, is_active=is_active, q=q.strip() if q else None))


@router.post(
    "/branches",
    status_code=status.HTTP_201_CREATED,
    summary="Create a branch",
    description=(
        "`name` is up to 40 characters and unique per practice, ignoring case (409 "
        "`BRANCH_NAME_TAKEN`). Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_branch(
    body: BranchCreate, org: BranchManager, service: BranchServiceDep, ctx: ContextDep
) -> ApiResponse[BranchRead]:
    return ok(await service.create(org, body, ctx), "Branch created")


@router.get(
    "/branches/{branch_id}",
    summary="Get a branch",
    description="One branch with its client count. Any member.",
    responses=error_responses(404),
)
async def get_branch(
    branch_id: uuid.UUID, org: OrgContextDep, service: BranchServiceDep
) -> ApiResponse[BranchRead]:
    return ok(await service.get(org, branch_id))


@router.patch(
    "/branches/{branch_id}",
    summary="Update a branch",
    description=(
        "Changes the name or the active state. 409 `BRANCH_NAME_TAKEN` for a duplicate; 409 "
        "`LAST_ACTIVE_BRANCH` for deactivating the last active branch while the practice uses "
        "branches. Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_branch(
    branch_id: uuid.UUID,
    body: BranchUpdate,
    org: BranchManager,
    service: BranchServiceDep,
    ctx: ContextDep,
) -> ApiResponse[BranchRead]:
    return ok(await service.update(org, branch_id, body, ctx), "Branch updated")


@router.post(
    "/branches/{branch_id}/deactivate",
    summary="Deactivate a branch",
    description=(
        "A deactivated branch is not offered for new clients; clients that have it keep it. "
        "409 `LAST_ACTIVE_BRANCH` for the last active branch while the practice uses branches. "
        "Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def deactivate_branch(
    branch_id: uuid.UUID, org: BranchManager, service: BranchServiceDep, ctx: ContextDep
) -> ApiResponse[BranchRead]:
    return ok(await service.set_active(org, branch_id, False, ctx), "Branch deactivated")


@router.post(
    "/branches/{branch_id}/activate",
    summary="Reactivate a branch",
    description="Makes a deactivated branch available again. Requires `settings.manage`.",
    responses=error_responses(403, 404),
)
async def activate_branch(
    branch_id: uuid.UUID, org: BranchManager, service: BranchServiceDep, ctx: ContextDep
) -> ApiResponse[BranchRead]:
    return ok(await service.set_active(org, branch_id, True, ctx), "Branch activated")


@router.delete(
    "/branches/{branch_id}",
    summary="Delete or deactivate a branch",
    description=(
        "Deletes a branch no client has. A branch that clients have (deleted clients included) "
        "is deactivated instead; `data.outcome` says which (`deleted` or `deactivated`). "
        "409 `LAST_ACTIVE_BRANCH` for the last active branch while the practice uses branches. "
        "Requires `settings.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_branch(
    branch_id: uuid.UUID, org: BranchManager, service: BranchServiceDep, ctx: ContextDep
) -> ApiResponse[DeleteOutcome]:
    outcome = await service.delete(org, branch_id, ctx)
    message = (
        "Branch deleted"
        if outcome.outcome == "deleted"
        else "Branch is in use, so it was deactivated"
    )
    return ok(outcome, message)
