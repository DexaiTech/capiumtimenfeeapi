"""Lookups of the records time and jobs refer to: clients, services, activity codes, members.

Every method is scoped to one organization; a row of another practice is never returned.
"""

import uuid
from collections import defaultdict
from collections.abc import Collection
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ActivityCode,
    Client,
    OrganizationMember,
    Service,
    ServiceActivityCode,
    User,
)


class TimeReferenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_client(self, organization_id: uuid.UUID, client_id: uuid.UUID) -> Client | None:
        """A live (not deleted) client."""
        stmt = select(Client).where(
            Client.organization_id == organization_id,
            Client.id == client_id,
            Client.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_clients(
        self, organization_id: uuid.UUID, client_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, Client]:
        if not client_ids:
            return {}
        stmt = select(Client).where(
            Client.organization_id == organization_id,
            Client.id.in_(list(client_ids)),
            Client.deleted_at.is_(None),
        )
        return {c.id: c for c in (await self.session.execute(stmt)).scalars()}

    async def get_service(
        self, organization_id: uuid.UUID, service_id: uuid.UUID
    ) -> Service | None:
        stmt = select(Service).where(
            Service.organization_id == organization_id, Service.id == service_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_activity_codes(
        self, organization_id: uuid.UUID, ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, ActivityCode]:
        if not ids:
            return {}
        stmt = select(ActivityCode).where(
            ActivityCode.organization_id == organization_id, ActivityCode.id.in_(list(ids))
        )
        return {a.id: a for a in (await self.session.execute(stmt)).scalars()}

    async def get_member(
        self, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> OrganizationMember | None:
        stmt = select(OrganizationMember).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def active_member_ids(
        self, organization_id: uuid.UUID, user_ids: Collection[uuid.UUID]
    ) -> set[uuid.UUID]:
        if not user_ids:
            return set()
        stmt = select(OrganizationMember.user_id).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id.in_(list(user_ids)),
            OrganizationMember.status == "active",
        )
        return set((await self.session.execute(stmt)).scalars())

    async def leaving_dates(
        self, organization_id: uuid.UUID, user_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, date]:
        """The leaving date of each of these members that has one."""
        if not user_ids:
            return {}
        stmt = select(OrganizationMember.user_id, OrganizationMember.leaving_date).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id.in_(list(user_ids)),
            OrganizationMember.leaving_date.is_not(None),
        )
        return {
            row[0]: row[1] for row in (await self.session.execute(stmt)).all() if row[1] is not None
        }

    async def allowed_activities(
        self, organization_id: uuid.UUID, service_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, set[uuid.UUID]]:
        """Per service, the activity codes it is limited to. A service without an entry allows
        every activity code."""
        if not service_ids:
            return {}
        stmt = select(ServiceActivityCode.service_id, ServiceActivityCode.activity_code_id).where(
            ServiceActivityCode.organization_id == organization_id,
            ServiceActivityCode.service_id.in_(list(service_ids)),
        )
        allowed: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        for service_id, activity_id in (await self.session.execute(stmt)).all():
            allowed[service_id].add(activity_id)
        return dict(allowed)

    async def managed_user_ids(
        self, organization_id: uuid.UUID, manager_user_id: uuid.UUID
    ) -> set[uuid.UUID]:
        """The people whose timesheets ``manager_user_id`` approves."""
        stmt = select(OrganizationMember.user_id).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.manager_user_id == manager_user_id,
        )
        return set((await self.session.execute(stmt)).scalars())

    async def user_names(
        self, organization_id: uuid.UUID, user_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        """Full names of members (any status) of the organization."""
        if not user_ids:
            return {}
        stmt = (
            select(User.id, User.full_name)
            .join(OrganizationMember, OrganizationMember.user_id == User.id)
            .where(
                OrganizationMember.organization_id == organization_id,
                User.id.in_(list(user_ids)),
            )
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}
