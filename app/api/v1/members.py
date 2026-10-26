"""Staff (member) routes. HTTP only: every rule lives in ``MemberService``."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.dependencies.common import ContextDep, SessionDep, SettingsDep
from app.dependencies.organization import OrgContextDep, PageParamsDep, require
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.member_permissions import MemberPermissionsRead, MemberPermissionsUpdate
from app.schemas.organization import MemberRead, MemberStatus, MemberUpdate
from app.services.member_permissions import MemberPermissionService
from app.services.members import MemberService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}/members", tags=["Members"])


def get_member_service(session: SessionDep) -> MemberService:
    return MemberService(session)


MemberServiceDep = Annotated[MemberService, Depends(get_member_service)]


def get_member_permission_service(session: SessionDep) -> MemberPermissionService:
    return MemberPermissionService(session)


MemberPermissionServiceDep = Annotated[
    MemberPermissionService, Depends(get_member_permission_service)
]


@router.get(
    "",
    summary="List members",
    description=(
        "Members ordered by name, cursor-paginated. Without `status` active and suspended "
        "members are returned. `q` matches the name or email, case-insensitively. "
        "Requires `member.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_members(
    org: Annotated[OrgContext, Depends(require("member.read"))],
    service: MemberServiceDep,
    settings: SettingsDep,
    page: PageParamsDep,
    status: Annotated[
        list[MemberStatus] | None,
        Query(description="Membership status; repeat for several. Default: active and suspended"),
    ] = None,
    role_id: Annotated[uuid.UUID | None, Query(description="Only members with this role")] = None,
    q: Annotated[str | None, Query(max_length=200, description="Search in name and email")] = None,
) -> ApiResponse[list[MemberRead]]:
    result = await service.list_members(org, statuses=status, role_id=role_id, q=q, params=page)
    return ok_page([MemberRead.from_model(m) for m in result.items], result.next_cursor)


@router.get(
    "/{user_id}",
    summary="Get a member",
    description="One member of the practice by user id. Requires `member.read`.",
    responses=error_responses(403, 404),
)
async def get_member(
    user_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require("member.read"))],
    service: MemberServiceDep,
    settings: SettingsDep,
) -> ApiResponse[MemberRead]:
    member = await service.get(org, user_id)
    return ok(MemberRead.from_model(member))


@router.patch(
    "/{user_id}",
    summary="Update a member",
    description=(
        "Changes a member's role, status, manager (who approves their timesheets), weekly "
        "capacity, chargeable target (basis points, 7500 = 75%), job title or initials. "
        "Initials are upper-cased and unique among the practice's members, case-insensitively "
        '(409 `INITIALS_TAKEN`); `job_title` and `initials` clear with `null` or `""`. The last '
        "active Owner cannot be demoted or suspended (409), only an Owner can give or take "
        "away the Owner role, a caller can assign only roles whose permissions they hold, and "
        "nobody changes their own role or status (403). Requires `member.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_member(
    user_id: uuid.UUID,
    body: MemberUpdate,
    org: Annotated[OrgContext, Depends(require("member.update"))],
    service: MemberServiceDep,
    settings: SettingsDep,
    ctx: ContextDep,
) -> ApiResponse[MemberRead]:
    member = await service.update(org, user_id, body, ctx)
    return ok(MemberRead.from_model(member), "Member updated")


@router.delete(
    "/{user_id}",
    summary="Remove a member or leave",
    description=(
        "Sets the member's status to `removed`; the row is kept. Requires `member.remove`, "
        "except when removing oneself, which is how a member leaves. The last active Owner "
        "cannot be removed (409)."
    ),
    responses=error_responses(403, 404, 409),
)
async def remove_member(
    user_id: uuid.UUID, org: OrgContextDep, service: MemberServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.remove(org, user_id, ctx)
    left = user_id == org.user.id
    return ok(message="You left the practice" if left else "Member removed")


@router.get(
    "/{user_id}/permissions",
    summary="A member's permissions",
    description=(
        "What the member's role gives them (`role_permissions`), what is granted to them on top "
        "(`grants`), what is taken from them (`denies`) and what they can do (`effective`). "
        "Requires `role.read` and `member.read`."
    ),
    responses=error_responses(403, 404),
)
async def get_member_permissions(
    user_id: uuid.UUID,
    org: Annotated[OrgContext, Depends(require("role.read", "member.read"))],
    service: MemberPermissionServiceDep,
) -> ApiResponse[MemberPermissionsRead]:
    return ok(await service.get(org, user_id))


@router.put(
    "/{user_id}/permissions",
    summary="Change a member's own permissions",
    description=(
        "Replaces the member's grants and denials: `grants` are given on top of their role, "
        "`denies` are taken from them although their role gives them. Only what differs from the "
        "role is kept. You can grant only what you hold yourself (403), not change your own "
        "(403) or an Owner's (409 `OWNER_PERMISSIONS_FIXED`), and a code cannot be both "
        "granted and denied (422). Audited as `member.permissions_changed`. Requires "
        "`role.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def set_member_permissions(
    user_id: uuid.UUID,
    body: MemberPermissionsUpdate,
    org: Annotated[OrgContext, Depends(require("role.manage"))],
    service: MemberPermissionServiceDep,
    ctx: ContextDep,
) -> ApiResponse[MemberPermissionsRead]:
    return ok(await service.update(org, user_id, body, ctx), "Permissions saved")
