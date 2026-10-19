"""Queries for the integration log and the change-feed cursors. Every method is scoped to one
organization."""

import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, func, literal_column, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IntegrationCursor, IntegrationSyncLog
from app.utils.pagination import Page, PageParams, paginate


@dataclass(frozen=True, slots=True)
class SyncLogFilters:
    integration: str | None = None
    status: str | None = None
    entity_id: uuid.UUID | None = None


class SyncLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, entry: IntegrationSyncLog) -> IntegrationSyncLog:
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def event_processed(
        self, organization_id: uuid.UUID, integration: str, event_id: str
    ) -> bool:
        """Whether an event was already applied or skipped (a row carries its id)."""
        stmt = select(IntegrationSyncLog.id).where(
            IntegrationSyncLog.organization_id == organization_id,
            IntegrationSyncLog.integration == integration,
            IntegrationSyncLog.external_event_id == event_id,
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None

    async def failures_since_success(
        self,
        organization_id: uuid.UUID,
        integration: str,
        operation: str,
        *,
        entity_id: uuid.UUID | None,
        external_id: str | None,
    ) -> int:
        """Failed attempts of the same operation on the same record since it last worked:
        the next attempt is one more than this."""
        same_record: list[ColumnElement[bool]] = [
            IntegrationSyncLog.organization_id == organization_id,
            IntegrationSyncLog.integration == integration,
            IntegrationSyncLog.operation == operation,
            IntegrationSyncLog.entity_id == entity_id
            if entity_id is not None
            else IntegrationSyncLog.entity_id.is_(None),
        ]
        if external_id is not None:
            same_record.append(IntegrationSyncLog.external_id == external_id)
        last_good = (
            select(func.max(IntegrationSyncLog.created_at))
            .where(*same_record, IntegrationSyncLog.status != "failed")
            .scalar_subquery()
        )
        stmt = select(func.count()).where(
            *same_record,
            IntegrationSyncLog.status == "failed",
            IntegrationSyncLog.created_at
            > func.coalesce(last_good, literal_column("'-infinity'::timestamptz")),
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def list_page(
        self, organization_id: uuid.UUID, filters: SyncLogFilters, params: PageParams
    ) -> Page[IntegrationSyncLog]:
        """Newest first."""
        stmt = select(IntegrationSyncLog).where(
            IntegrationSyncLog.organization_id == organization_id
        )
        if filters.integration is not None:
            stmt = stmt.where(IntegrationSyncLog.integration == filters.integration)
        if filters.status is not None:
            stmt = stmt.where(IntegrationSyncLog.status == filters.status)
        if filters.entity_id is not None:
            stmt = stmt.where(IntegrationSyncLog.entity_id == filters.entity_id)
        return await paginate(
            self.session,
            stmt,
            sort_column=IntegrationSyncLog.created_at,
            id_column=IntegrationSyncLog.id,
            params=params,
            descending=True,
        )


class CursorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, organization_id: uuid.UUID, integration: str) -> str | None:
        stmt = select(IntegrationCursor.cursor).where(
            IntegrationCursor.organization_id == organization_id,
            IntegrationCursor.integration == integration,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def save(self, organization_id: uuid.UUID, integration: str, cursor: str) -> None:
        stmt = insert(IntegrationCursor).values(
            organization_id=organization_id, integration=integration, cursor=cursor
        )
        await self.session.execute(
            stmt.on_conflict_do_update(
                index_elements=["organization_id", "integration"],
                set_={"cursor": stmt.excluded.cursor, "updated_at": func.now()},
            )
        )
