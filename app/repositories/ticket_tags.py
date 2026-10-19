"""Ticket tag queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Collection

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TicketTag, TicketTagLink


class TicketTagRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, tag: TicketTag) -> TicketTag:
        self.session.add(tag)
        await self.session.flush()
        await self.session.refresh(tag)
        return tag

    async def save(self, tag: TicketTag) -> TicketTag:
        await self.session.flush()
        await self.session.refresh(tag)
        return tag

    async def get(
        self, organization_id: uuid.UUID, tag_id: uuid.UUID, *, lock: bool = False
    ) -> TicketTag | None:
        stmt = (
            select(TicketTag)
            .where(TicketTag.organization_id == organization_id, TicketTag.id == tag_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_many(
        self, organization_id: uuid.UUID, tag_ids: Collection[uuid.UUID]
    ) -> list[TicketTag]:
        if not tag_ids:
            return []
        stmt = select(TicketTag).where(
            TicketTag.organization_id == organization_id, TicketTag.id.in_(tag_ids)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def list_with_counts(self, organization_id: uuid.UUID) -> list[tuple[TicketTag, int]]:
        """Every tag of the practice by name, with the number of tickets that carry it."""
        used = (
            select(func.count())
            .select_from(TicketTagLink)
            .where(
                TicketTagLink.organization_id == TicketTag.organization_id,
                TicketTagLink.tag_id == TicketTag.id,
            )
            .correlate(TicketTag)
            .scalar_subquery()
        )
        stmt = (
            select(TicketTag, used.label("ticket_count"))
            .where(TicketTag.organization_id == organization_id)
            .order_by(func.lower(TicketTag.name), TicketTag.id)
        )
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def count(self, organization_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(TicketTag.organization_id == organization_id)
        return int((await self.session.execute(stmt)).scalar_one())

    async def name_taken(
        self, organization_id: uuid.UUID, name: str, *, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = select(TicketTag.id).where(
            TicketTag.organization_id == organization_id,
            func.lower(TicketTag.name) == name.lower(),
        )
        if exclude_id is not None:
            stmt = stmt.where(TicketTag.id != exclude_id)
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def usage(self, organization_id: uuid.UUID, tag_id: uuid.UUID) -> int:
        """Tickets that carry the tag."""
        stmt = select(func.count()).where(
            TicketTagLink.organization_id == organization_id, TicketTagLink.tag_id == tag_id
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def delete(self, tag: TicketTag) -> None:
        """Delete the tag; its links go with it."""
        await self.session.delete(tag)
        await self.session.flush()
