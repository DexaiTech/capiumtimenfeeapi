"""Period-lock queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Collection
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LockedPeriod, PeriodLock, PeriodLockEvent, User
from app.utils.ids import new_id
from app.utils.pagination import Page, PageParams, paginate


class PeriodLockRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def locked_periods_shared(self, organization_id: uuid.UUID) -> list[LockedPeriod]:
        """The practice's locked periods, with its lock row share-locked until the transaction ends.

        A lock or unlock needs the row ``FOR UPDATE``: it waits for the writes that were checked
        against the old state, and a write that starts after it waits for it.
        """
        # Two statements on purpose: a write that waited for the lock must read the periods with
        # a snapshot taken after the wait, or it would miss the period that was just locked.
        await self.session.execute(
            select(PeriodLock.id)
            .where(PeriodLock.organization_id == organization_id)
            .with_for_update(read=True)
        )
        return await self.list_periods(organization_id)

    async def list_periods(self, organization_id: uuid.UUID) -> list[LockedPeriod]:
        """Oldest period first."""
        stmt = (
            select(LockedPeriod)
            .where(LockedPeriod.organization_id == organization_id)
            .order_by(LockedPeriod.start_date, LockedPeriod.end_date)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get_period(
        self, organization_id: uuid.UUID, period_id: uuid.UUID
    ) -> LockedPeriod | None:
        stmt = select(LockedPeriod).where(
            LockedPeriod.organization_id == organization_id, LockedPeriod.id == period_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def overlapping(
        self, organization_id: uuid.UUID, start: date, end: date
    ) -> LockedPeriod | None:
        stmt = (
            select(LockedPeriod)
            .where(
                LockedPeriod.organization_id == organization_id,
                LockedPeriod.start_date <= end,
                LockedPeriod.end_date >= start,
            )
            .order_by(LockedPeriod.start_date)
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_period(self, period: LockedPeriod) -> LockedPeriod:
        self.session.add(period)
        await self.session.flush()
        await self.session.refresh(period)
        return period

    async def delete_period(self, period: LockedPeriod) -> None:
        await self.session.delete(period)
        await self.session.flush()

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
