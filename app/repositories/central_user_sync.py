"""Queries behind the central user integration. Every method is scoped to one organization except the
lookup of a user by email (users belong to no practice)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrganizationMember, Permission, RolePermission, User

# What it takes to run the practice's staff and roles: the last person who holds both of these
# is never deactivated by a central event.
ADMINISTRATOR_PERMISSIONS = ("role.manage", "member.update")


class CentralUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def user_by_email(self, email: str) -> User | None:
        stmt = select(User).where(
            func.lower(User.email) == email.strip().lower(), User.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def is_administrator(self, member: OrganizationMember) -> bool:
        """Whether the member's role grants every administrator permission."""
        stmt = (
            select(func.count(func.distinct(Permission.code)))
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .where(
                RolePermission.role_id == member.role_id,
                Permission.code.in_(ADMINISTRATOR_PERMISSIONS),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one()) == len(ADMINISTRATOR_PERMISSIONS)

    async def other_administrators(
        self, organization_id: uuid.UUID, exclude_user_id: uuid.UUID
    ) -> int:
        """Active staff who can sign in and hold every administrator permission, except one."""
        administrator_roles = (
            select(RolePermission.role_id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(Permission.code.in_(ADMINISTRATOR_PERMISSIONS))
            .group_by(RolePermission.role_id)
            .having(func.count(func.distinct(Permission.code)) == len(ADMINISTRATOR_PERMISSIONS))
        )
        stmt = select(func.count()).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.status == "active",
            OrganizationMember.login_enabled.is_(True),
            OrganizationMember.user_id != exclude_user_id,
            OrganizationMember.role_id.in_(administrator_roles),
        )
        return int((await self.session.execute(stmt)).scalar_one())
