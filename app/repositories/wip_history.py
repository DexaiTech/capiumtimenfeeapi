"""Frozen WIP snapshots and the ledger movements the historic report adds to them."""

import uuid
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WipMovement, WipSnapshot, WipSnapshotItem


class WipHistoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_snapshot(self, organization_id: uuid.UUID, as_at: date) -> WipSnapshot | None:
        stmt = select(WipSnapshot).where(
            WipSnapshot.organization_id == organization_id, WipSnapshot.as_at == as_at
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def add_snapshot(self, snapshot: WipSnapshot, items: list[WipSnapshotItem]) -> None:
        self.session.add(snapshot)
        await self.session.flush()
        self.session.add_all(items)
        await self.session.flush()

    async def snapshot_items(
        self, organization_id: uuid.UUID, snapshot_id: uuid.UUID
    ) -> list[WipSnapshotItem]:
        stmt = (
            select(WipSnapshotItem)
            .where(
                WipSnapshotItem.organization_id == organization_id,
                WipSnapshotItem.snapshot_id == snapshot_id,
            )
            .order_by(WipSnapshotItem.item_date, WipSnapshotItem.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def movements_until(
        self,
        organization_id: uuid.UUID,
        as_at: date,
        recorded_before: datetime | None = None,
    ) -> list[WipMovement]:
        """The ledger movements effective on or before ``as_at``, oldest first. With
        ``recorded_before`` only those written by then: a movement dated back into a day that
        was already frozen does not change it."""
        stmt = select(WipMovement).where(
            WipMovement.organization_id == organization_id,
            WipMovement.effective_date <= as_at,
        )
        if recorded_before is not None:
            stmt = stmt.where(WipMovement.created_at <= recorded_before)
        stmt = stmt.order_by(WipMovement.created_at, WipMovement.id)
        return list((await self.session.execute(stmt)).scalars())
