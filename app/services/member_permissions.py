"""A person's own permissions: grants and denials on top of their role (Team > Staff).

A person's permissions are their role's, plus what is granted to them, less what is denied. The
role stays the starting point: an override is stored only where it differs from it (a grant of
something the role gives, or a denial of something it does not, is dropped). Owners hold every
permission always, so an Owner's cannot be changed, and nobody changes their own.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError, ConflictError, NotFoundError, ValidationError
from app.core.permissions import OWNER_KEY, effective_permissions
from app.db.models import OrganizationMember
from app.db.session import unit_of_work
from app.repositories.member_permissions import MemberPermissionRepository
from app.repositories.members import MemberRepository
from app.repositories.timesheets import TimesheetRepository
from app.schemas.member_permissions import MemberPermissionsRead, MemberPermissionsUpdate
from app.schemas.organization import RoleSummary
from app.services import audit, policy
from app.services.organizations import lock_and_refresh
from app.services.policy import OrgContext


def _read(member: OrganizationMember, overrides: dict[str, str]) -> MemberPermissionsRead:
    role_codes = sorted(p.code for p in member.role.permissions)
    return MemberPermissionsRead(
        user_id=member.user_id,
        role=RoleSummary.from_model(member.role),
        role_permissions=role_codes,
        grants=sorted(code for code, effect in overrides.items() if effect == "grant"),
        denies=sorted(code for code, effect in overrides.items() if effect == "deny"),
        effective=sorted(effective_permissions(role_codes, overrides.items())),
    )


class MemberPermissionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.members = MemberRepository(session)
        self.repo = MemberPermissionRepository(session)

    async def get(self, ctx: OrgContext, user_id: uuid.UUID) -> MemberPermissionsRead:
        member = await self.members.get(ctx.organization_id, user_id)
        if member is None:
            raise NotFoundError("Member not found.")
        return _read(member, await self.repo.overrides(ctx.organization_id, user_id))

    async def update(
        self,
        ctx: OrgContext,
        user_id: uuid.UUID,
        data: MemberPermissionsUpdate,
        rctx: RequestContext,
    ) -> MemberPermissionsRead:
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "role.manage")
            member = await self.members.get(ctx.organization_id, user_id)
            if member is None:
                raise NotFoundError("Member not found.")
            if member.user_id == ctx.user.id:
                raise AuthorizationError("You cannot change your own permissions.")
            if member.role.system_key == OWNER_KEY:
                raise ConflictError(
                    "An Owner holds every permission, always.", code="OWNER_PERMISSIONS_FIXED"
                )
            grants, denies = set(data.grants), set(data.denies)
            both = sorted(grants & denies)
            if both:
                raise ValidationError.for_field(
                    "denies", f"A permission cannot be both granted and denied: {', '.join(both)}."
                )
            unknown = sorted((grants | denies) - await self.repo.known_codes())
            if unknown:
                raise ValidationError.for_field(
                    "grants", f"Unknown permission: {', '.join(unknown)}."
                )
            role_codes = {p.code for p in member.role.permissions}
            # Only what differs from the role is kept.
            grants -= role_codes
            denies &= role_codes
            # Nobody hands out permissions they do not hold themselves.
            policy.ensure_can_grant(ctx, grants)
            old = await self.repo.overrides(ctx.organization_id, user_id)
            new = {**dict.fromkeys(grants, "grant"), **dict.fromkeys(denies, "deny")}
            if old != new:
                await self.repo.replace(ctx.organization_id, user_id, new, ctx.user.id)
                # Whether their time needs approval follows their permissions.
                await TimesheetRepository(self.session).sync_exemptions(
                    ctx.organization_id, user_id
                )
                meta: dict[str, Any] = {
                    "member_name": member.user.full_name,
                    "grants_added": sorted(
                        c for c, e in new.items() if e == "grant" and old.get(c) != "grant"
                    ),
                    "grants_removed": sorted(
                        c for c, e in old.items() if e == "grant" and new.get(c) != "grant"
                    ),
                    "denies_added": sorted(
                        c for c, e in new.items() if e == "deny" and old.get(c) != "deny"
                    ),
                    "denies_removed": sorted(
                        c for c, e in old.items() if e == "deny" and new.get(c) != "deny"
                    ),
                }
                await audit.record(
                    self.session,
                    "member.permissions_changed",
                    ctx=rctx,
                    actor_user_id=ctx.user.id,
                    organization_id=ctx.organization_id,
                    target_type="member",
                    target_id=member.user_id,
                    meta=meta,
                )
        return _read(member, new if old != new else old)
