"""Period-lock queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Collection
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PeriodLock, PeriodLockEvent, User
from app.utils.ids import new_id
from app.utils.pagination import Page, PageParams, paginate


class PeriodLockRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def locked_through_shared(self, organization_id: uuid.UUID) -> date | None:
        """The last locked day, with the row share-locked until the transaction ends.

        A lock or reopening needs the row ``FOR UPDATE``: it waits for the writes that were
        checked against the old state, and a write that starts after it waits for it.
        """
        stmt = (
            select(PeriodLock.locked_through)
            .where(PeriodLock.organization_id == organization_id)
            .with_for_update(read=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get(self, organization_id: uuid.UUID) -> PeriodLock | None:
        stmt = (
            select(PeriodLock)
            .where(PeriodLock.organization_id == organization_id)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_row(self, organization_id: uuid.UUID) -> PeriodLock:
        """The practice's row ``FOR UPDATE``, created first when a practice has none yet."""
        await self.session.execute(
            insert(PeriodLock)
            .values(id=new_id(), organization_id=organization_id)
            .on_conflict_do_nothing(index_elements=["organization_id"])
        )
        stmt = (
            select(PeriodLock)
            .where(PeriodLock.organization_id == organization_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def add_event(self, event: PeriodLockEvent) -> PeriodLockEvent:
        self.session.add(event)
        await self.session.flush()
        await self.session.refresh(event)
        return event

    async def last_event(self, organization_id: uuid.UUID) -> PeriodLockEvent | None:
        stmt = (
            select(PeriodLockEvent)
            .where(PeriodLockEvent.organization_id == organization_id)
            .order_by(PeriodLockEvent.created_at.desc(), PeriodLockEvent.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_events(
        self, organization_id: uuid.UUID, params: PageParams
    ) -> Page[PeriodLockEvent]:
        """Newest first."""
        return await paginate(
            self.session,
            select(PeriodLockEvent).where(PeriodLockEvent.organization_id == organization_id),
            sort_column=PeriodLockEvent.created_at,
            id_column=PeriodLockEvent.id,
            params=params,
            descending=True,
        )

    async def actor_names(self, user_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Names of people taken from this practice's own events (their ids are members)."""
        if not user_ids:
            return {}
        rows = await self.session.execute(
            select(User.id, User.full_name).where(User.id.in_(set(user_ids)))
        )
        return {row.id: row.full_name for row in rows}
