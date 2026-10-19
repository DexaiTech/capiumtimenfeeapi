"""Role and permission routes. HTTP only: every rule lives in ``RoleService``."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.dependencies.auth import VerifiedPrincipalDep
from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import require
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.organization import (
    PermissionRead,
    RoleCreate,
    RolePermissionsUpdate,
    RoleRead,
    RoleUpdate,
)
from app.services.policy import OrgContext
from app.services.roles import RoleService

router = APIRouter(prefix="/organizations/{organization_id}/roles", tags=["Roles"])
permissions_router = APIRouter(prefix="/permissions", tags=["Roles"])

RoleManager = Annotated[OrgContext, Depends(require("role.manage"))]


def get_role_service(session: SessionDep) -> RoleService:
    return RoleService(session)


RoleServiceDep = Annotated[RoleService, Depends(get_role_service)]


@permissions_router.get(
    "",
    summary="List the permission catalogue",
    description="Every permission a role can hold, ordered by category then code. Not paginated.",
    responses=error_responses(403),
)
async def list_permissions(
    _: VerifiedPrincipalDep, service: RoleServiceDep
) -> ApiResponse[list[PermissionRead]]:
    return ok([PermissionRead.from_model(p) for p in await service.list_permissions()])


@router.get(
    "",
    summary="List roles",
    description=(
        "System roles first in their fixed order, then custom roles by name, each with its "
        "permissions and member count. Not paginated: the set is small and bounded. "
        "Requires `role.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_roles(
    org: Annotated[OrgContext, Depends(require("role.read"))], service: RoleServiceDep
) -> ApiResponse[list[RoleRead]]:
    return ok([RoleRead.from_model(role, count) for role, count in await service.list_roles(org)])


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a custom role",
    description=(
        "Creates a role from catalogue permissions. The caller must hold every permission "
        "they put in it (403). 409 on a duplicate name, 422 for an unknown permission code. "
        "Requires `role.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_role(
    body: RoleCreate, org: RoleManager, service: RoleServiceDep, ctx: ContextDep
) -> ApiResponse[RoleRead]:
    role, count = await service.create(org, body, ctx)
    return ok(RoleRead.from_model(role, count), "Role created")


@router.patch(
    "/{role_id}",
    summary="Rename or describe a custom role",
    description="System roles cannot be edited (409). Requires `role.manage`.",
    responses=error_responses(403, 404, 409),
)
async def update_role(
    role_id: uuid.UUID,
    body: RoleUpdate,
    org: RoleManager,
    service: RoleServiceDep,
    ctx: ContextDep,
) -> ApiResponse[RoleRead]:
    role, count = await service.update(org, role_id, body, ctx)
    return ok(RoleRead.from_model(role, count), "Role updated")


@router.put(
    "/{role_id}/permissions",
    summary="Replace a custom role's permissions",
    description=(
        "Replaces the whole permission set. System roles cannot be edited (409). The caller "
        "must hold every permission in both the old and the new set (403). "
        "Requires `role.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def set_role_permissions(
    role_id: uuid.UUID,
    body: RolePermissionsUpdate,
    org: RoleManager,
    service: RoleServiceDep,
    ctx: ContextDep,
) -> ApiResponse[RoleRead]:
    role, count = await service.set_permissions(org, role_id, body, ctx)
    return ok(RoleRead.from_model(role, count), "Permissions updated")


@router.delete(
    "/{role_id}",
    summary="Delete a custom role",
    description=(
        "Soft-deletes the role. System roles cannot be deleted, nor can a role that still "
        "has members (409). Requires `role.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_role(
    role_id: uuid.UUID, org: RoleManager, service: RoleServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.delete(org, role_id, ctx)
    return ok(message="Role deleted")
