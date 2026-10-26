"""A person's own permissions (grants and denials on top of their role)."""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MemberPermissionOverride, Permission


class MemberPermissionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def overrides(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, str]:
        """``{code: 'grant' | 'deny'}`` of the member."""
        stmt = select(
            MemberPermissionOverride.permission_code, MemberPermissionOverride.effect
        ).where(
            MemberPermissionOverride.organization_id == organization_id,
            MemberPermissionOverride.user_id == user_id,
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

    async def known_codes(self) -> set[str]:
        return set((await self.session.execute(select(Permission.code))).scalars())

    async def replace(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        overrides: dict[str, str],
        created_by: uuid.UUID,
    ) -> None:
        await self.session.execute(
            delete(MemberPermissionOverride).where(
                MemberPermissionOverride.organization_id == organization_id,
                MemberPermissionOverride.user_id == user_id,
            )
        )
        self.session.add_all(
            MemberPermissionOverride(
                organization_id=organization_id,
                user_id=user_id,
                permission_code=code,
                effect=effect,
                created_by=created_by,
            )
            for code, effect in sorted(overrides.items())
        )
        await self.session.flush()
