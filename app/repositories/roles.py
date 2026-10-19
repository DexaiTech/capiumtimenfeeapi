"""Role and permission queries. Role methods are scoped to one organization."""

import uuid
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import OrganizationMember, Permission, Role
from app.db.models.organization import GONE_STATUSES


class RoleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, role: Role) -> Role:
        self.session.add(role)
        await self.session.flush()
        return role

    async def list_permissions(self) -> list[Permission]:
        """The global catalogue, ordered by category then code."""
        stmt = select(Permission).order_by(Permission.category, Permission.code)
        return list((await self.session.execute(stmt)).scalars())

    async def permissions_by_code(self, codes: Iterable[str]) -> list[Permission]:
        stmt = select(Permission).where(Permission.code.in_(set(codes))).order_by(Permission.code)
        return list((await self.session.execute(stmt)).scalars())

    async def list_for_organization(self, organization_id: uuid.UUID) -> list[Role]:
        stmt = (
            select(Role)
            .where(Role.organization_id == organization_id, Role.deleted_at.is_(None))
            .options(selectinload(Role.permissions))
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get(self, organization_id: uuid.UUID, role_id: uuid.UUID) -> Role | None:
        """A live role of this organization, read fresh, with its permissions."""
        stmt = (
            select(Role)
            .where(
                Role.organization_id == organization_id,
                Role.id == role_id,
                Role.deleted_at.is_(None),
            )
            .options(selectinload(Role.permissions))
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_system_key(self, organization_id: uuid.UUID, system_key: str) -> Role | None:
        """The practice's copy of a system role (``staff``, ``admin``, ...) with its permissions."""
        stmt = (
            select(Role)
            .where(
                Role.organization_id == organization_id,
                Role.system_key == system_key,
                Role.deleted_at.is_(None),
            )
            .options(selectinload(Role.permissions))
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def name_taken(
        self, organization_id: uuid.UUID, name: str, *, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = select(Role.id).where(
            Role.organization_id == organization_id,
            func.lower(Role.name) == name.lower(),
            Role.deleted_at.is_(None),
        )
        if exclude_id is not None:
            stmt = stmt.where(Role.id != exclude_id)
        return (await self.session.execute(stmt)).first() is not None

    async def member_counts(
        self, organization_id: uuid.UUID, role_id: uuid.UUID | None = None
    ) -> dict[uuid.UUID, int]:
        """Members per role in one grouped query. Removed and departed staff are not counted."""
        stmt = (
            select(OrganizationMember.role_id, func.count())
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.status.not_in(GONE_STATUSES),
            )
            .group_by(OrganizationMember.role_id)
        )
        if role_id is not None:
            stmt = stmt.where(OrganizationMember.role_id == role_id)
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all()}
