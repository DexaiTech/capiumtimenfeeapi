"""Roles: the permission catalogue, the system roles and custom roles of an organization."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.permissions import SYSTEM_ROLES
from app.db.models import Permission, Role
from app.db.session import unit_of_work
from app.repositories.roles import RoleRepository
from app.schemas.organization import (
    RoleCreate,
    RolePermissionsUpdate,
    RoleUpdate,
)
from app.services import audit, policy
from app.services.organizations import lock_and_refresh
from app.services.policy import OrgContext
from app.utils.ids import new_id

_SYSTEM_ORDER = {role.key: index for index, role in enumerate(SYSTEM_ROLES)}


def _codes(role: Role) -> set[str]:
    return {p.code for p in role.permissions}


class RoleService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.roles = RoleRepository(session)

    async def list_permissions(self) -> list[Permission]:
        return await self.roles.list_permissions()

    async def list_roles(self, ctx: OrgContext) -> list[tuple[Role, int]]:
        """System roles in their fixed order, then custom roles by name, with member counts."""
        roles = await self.roles.list_for_organization(ctx.organization_id)
        counts = await self.roles.member_counts(ctx.organization_id)
        roles.sort(
            key=lambda r: (
                (0, _SYSTEM_ORDER.get(r.system_key, len(_SYSTEM_ORDER)), "")
                if r.system_key is not None
                else (1, 0, r.name.lower())
            )
        )
        return [(role, counts.get(role.id, 0)) for role in roles]

    async def create(
        self, ctx: OrgContext, data: RoleCreate, rctx: RequestContext
    ) -> tuple[Role, int]:
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "role.manage")
            permissions = await self._resolve(sorted(set(data.permissions)))
            policy.ensure_can_grant(ctx, (p.code for p in permissions))
            await self._ensure_name_free(ctx, data.name)
            role = await self.roles.add(
                Role(
                    id=new_id(),
                    organization_id=ctx.organization_id,
                    name=data.name,
                    description=data.description,
                    permissions=permissions,
                )
            )
            await self._audit(
                ctx,
                rctx,
                "role.created",
                role,
                {"name": role.name, "permissions": sorted(_codes(role))},
            )
        return role, 0

    async def update(
        self, ctx: OrgContext, role_id: uuid.UUID, data: RoleUpdate, rctx: RequestContext
    ) -> tuple[Role, int]:
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "role.manage")
            role = await self._get_custom(ctx, role_id)
            # Nobody edits a role that holds permissions they lack.
            policy.ensure_can_grant(ctx, _codes(role))
            changes: dict[str, list[Any]] = {}
            fields = data.model_fields_set
            if "name" in fields and data.name is not None and data.name != role.name:
                await self._ensure_name_free(ctx, data.name, exclude_id=role.id)
                changes["name"] = [role.name, data.name]
                role.name = data.name
            if "description" in fields and data.description != role.description:
                changes["description"] = [role.description, data.description]
                role.description = data.description
            if changes:
                await self.session.flush()
                await self._audit(ctx, rctx, "role.updated", role, {"changes": changes})
            count = await self._member_count(ctx, role)
        return role, count

    async def set_permissions(
        self,
        ctx: OrgContext,
        role_id: uuid.UUID,
        data: RolePermissionsUpdate,
        rctx: RequestContext,
    ) -> tuple[Role, int]:
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "role.manage")
            role = await self._get_custom(ctx, role_id)
            old = _codes(role)
            permissions = await self._resolve(sorted(set(data.permissions)))
            new = {p.code for p in permissions}
            policy.ensure_can_grant(ctx, old | new)
            if old != new:
                role.permissions = permissions
                await self.session.flush()
                await self._audit(
                    ctx,
                    rctx,
                    "role.permissions_changed",
                    role,
                    {"name": role.name, "added": sorted(new - old), "removed": sorted(old - new)},
                )
            count = await self._member_count(ctx, role)
        return role, count

    async def delete(self, ctx: OrgContext, role_id: uuid.UUID, rctx: RequestContext) -> None:
        async with unit_of_work(self.session):
            ctx = await lock_and_refresh(self.session, ctx, "role.manage")
            role = await self._get_custom(ctx, role_id)
            policy.ensure_can_grant(ctx, _codes(role))
            if await self._member_count(ctx, role):
                raise ConflictError(
                    "This role still has members. Move them to another role first.",
                    details={"code": "role_in_use"},
                )

            role.deleted_at = datetime.now(UTC)
            await self.session.flush()
            await self._audit(ctx, rctx, "role.deleted", role, {"name": role.name})

    async def _get_custom(self, ctx: OrgContext, role_id: uuid.UUID) -> Role:
        role = await self.roles.get(ctx.organization_id, role_id)
        if role is None:
            raise NotFoundError("Role not found.")
        if role.is_system:
            raise ConflictError("System roles cannot be changed.", details={"code": "system_role"})
        return role

    async def _resolve(self, codes: Sequence[str]) -> list[Permission]:
        permissions = await self.roles.permissions_by_code(codes)
        unknown = sorted(set(codes) - {p.code for p in permissions})
        if unknown:
            raise ValidationError.for_field(
                "permissions", f"Unknown permission: {', '.join(unknown)}"
            )
        return permissions

    async def _ensure_name_free(
        self, ctx: OrgContext, name: str, *, exclude_id: uuid.UUID | None = None
    ) -> None:
        if await self.roles.name_taken(ctx.organization_id, name, exclude_id=exclude_id):
            message = "A role with this name already exists."
            raise ConflictError(
                message, details=[{"field": "name", "code": "duplicate", "message": message}]
            )

    async def _member_count(self, ctx: OrgContext, role: Role) -> int:
        counts = await self.roles.member_counts(ctx.organization_id, role.id)
        return counts.get(role.id, 0)

    async def _audit(
        self, ctx: OrgContext, rctx: RequestContext, action: str, role: Role, meta: dict[str, Any]
    ) -> None:
        await audit.record(
            self.session,
            action,
            ctx=rctx,
            actor_user_id=ctx.user.id,
            organization_id=ctx.organization_id,
            target_type="role",
            target_id=role.id,
            meta=meta,
        )
