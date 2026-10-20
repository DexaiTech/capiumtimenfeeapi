"""Membership lookups used by the authorization dependency on every organization request."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.models import Organization, OrganizationMember, Role


class MembershipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_active(
        self, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[Organization, OrganizationMember] | None:
        """The organization and the caller's *active* membership, role and permissions loaded."""
        stmt = (
            select(Organization, OrganizationMember)
            .join(OrganizationMember, OrganizationMember.organization_id == Organization.id)
            .where(
                Organization.id == organization_id,
                Organization.deleted_at.is_(None),
                OrganizationMember.user_id == user_id,
                OrganizationMember.status == "active",
            )
            # One statement: the role and its permissions come with the row (two more round
            # trips as separate selectin loads).
            .options(joinedload(OrganizationMember.role).joinedload(Role.permissions))
        )
        row = (await self.session.execute(stmt)).unique().one_or_none()
        return (row[0], row[1]) if row else None
