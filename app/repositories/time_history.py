"""The correction history of time entries. Every method is scoped to one organization."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TimeEntryHistory
from app.utils.pagination import Page, PageParams, paginate


class TimeHistoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, row: TimeEntryHistory) -> TimeEntryHistory:
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_page(
        self, organization_id: uuid.UUID, time_entry_id: uuid.UUID, params: PageParams
    ) -> Page[TimeEntryHistory]:
        """Newest first."""
        stmt = select(TimeEntryHistory).where(
            TimeEntryHistory.organization_id == organization_id,
            TimeEntryHistory.time_entry_id == time_entry_id,
        )
        return await paginate(
            self.session,
            stmt,
            sort_column=TimeEntryHistory.created_at,
            id_column=TimeEntryHistory.id,
            params=params,
            descending=True,
        )

    async def owner_of(
        self, organization_id: uuid.UUID, time_entry_id: uuid.UUID
    ) -> uuid.UUID | None:
        """Whose time an entry was, from its history (the entry itself may be deleted)."""
        stmt = (
            select(TimeEntryHistory.user_id)
            .where(
                TimeEntryHistory.organization_id == organization_id,
                TimeEntryHistory.time_entry_id == time_entry_id,
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()
