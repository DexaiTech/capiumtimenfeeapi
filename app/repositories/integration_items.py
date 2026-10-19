"""Queries for the sync-item outbox and the integration connections. Every method is scoped to
one organization, except the ones that say they serve the worker (``due``,
``enabled_organizations``)."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IntegrationConnection, IntegrationSyncItem
from app.utils.ids import new_id
from app.utils.pagination import Page, PageParams, paginate

ITEM_KEY = ["organization_id", "integration", "idempotency_key"]


@dataclass(frozen=True, slots=True)
class SyncItemFilters:
    integration: str | None = None
    status: str | None = None
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    q: str | None = None


class SyncItemRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self, organization_id: uuid.UUID, item_id: uuid.UUID, *, lock: bool = False
    ) -> IntegrationSyncItem | None:
        stmt = select(IntegrationSyncItem).where(
            IntegrationSyncItem.organization_id == organization_id,
            IntegrationSyncItem.id == item_id,
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_key(
        self, organization_id: uuid.UUID, integration: str, idempotency_key: str
    ) -> IntegrationSyncItem | None:
        stmt = select(IntegrationSyncItem).where(
            IntegrationSyncItem.organization_id == organization_id,
            IntegrationSyncItem.integration == integration,
            IntegrationSyncItem.idempotency_key == idempotency_key,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def insert_once(self, values: dict[str, Any]) -> uuid.UUID | None:
        """Insert the item unless one with the same idempotency key exists; its id when this call
        created it, ``None`` when it was already there (nothing is changed then)."""
        stmt = (
            insert(IntegrationSyncItem)
            .values(**values)
            .on_conflict_do_nothing(index_elements=ITEM_KEY)
            .returning(IntegrationSyncItem.id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert_latest(self, values: dict[str, Any]) -> uuid.UUID:
        """Insert the item, or put the existing one with this key back to ``pending`` with the new
        payload (the latest change is what must reach the other system)."""
        stmt = insert(IntegrationSyncItem).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=ITEM_KEY,
            set_={
                "payload": stmt.excluded.payload,
                "entity_id": stmt.excluded.entity_id,
                "status": "pending",
                "attempts": 0,
                "next_attempt_at": stmt.excluded.next_attempt_at,
                "completed_at": None,
                "last_error": None,
                "last_error_code": None,
                "external_ref": None,
            },
        ).returning(IntegrationSyncItem.id)
        return (await self.session.execute(stmt)).scalar_one()

    async def list_page(
        self, organization_id: uuid.UUID, filters: SyncItemFilters, params: PageParams
    ) -> Page[IntegrationSyncItem]:
        """Newest first."""
        stmt = select(IntegrationSyncItem).where(
            IntegrationSyncItem.organization_id == organization_id
        )
        if filters.integration is not None:
            stmt = stmt.where(IntegrationSyncItem.integration == filters.integration)
        if filters.status is not None:
            stmt = stmt.where(IntegrationSyncItem.status == filters.status)
        if filters.entity_type is not None:
            stmt = stmt.where(IntegrationSyncItem.entity_type == filters.entity_type)
        if filters.entity_id is not None:
            stmt = stmt.where(IntegrationSyncItem.entity_id == filters.entity_id)
        if filters.q:
            stmt = stmt.where(
                or_(
                    IntegrationSyncItem.payload["label"].astext.icontains(
                        filters.q, autoescape=True
                    ),
                    IntegrationSyncItem.last_error.icontains(filters.q, autoescape=True),
                )
            )
        return await paginate(
            self.session,
            stmt,
            sort_column=IntegrationSyncItem.created_at,
            id_column=IntegrationSyncItem.id,
            params=params,
            descending=True,
        )

    async def due(
        self,
        *,
        limit: int,
        now: datetime,
        organization_id: uuid.UUID | None = None,
        integration: str | None = None,
    ) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """``(organization_id, item_id)`` of pending items whose time has come, oldest first, for
        practices where the integration is switched on. Serves the worker (every practice) and
        "run now" (one practice)."""
        stmt = (
            select(IntegrationSyncItem.organization_id, IntegrationSyncItem.id)
            .join(
                IntegrationConnection,
                and_(
                    IntegrationConnection.organization_id == IntegrationSyncItem.organization_id,
                    IntegrationConnection.integration == IntegrationSyncItem.integration,
                    IntegrationConnection.enabled.is_(True),
                ),
            )
            .where(
                IntegrationSyncItem.status == "pending",
                IntegrationSyncItem.next_attempt_at <= now,
            )
            .order_by(IntegrationSyncItem.next_attempt_at, IntegrationSyncItem.id)
            .limit(limit)
        )
        if organization_id is not None:
            stmt = stmt.where(IntegrationSyncItem.organization_id == organization_id)
        if integration is not None:
            stmt = stmt.where(IntegrationSyncItem.integration == integration)
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def lock_for_processing(
        self, organization_id: uuid.UUID, item_id: uuid.UUID, *, skip_locked: bool
    ) -> IntegrationSyncItem | None:
        """The item locked until the transaction ends, read fresh; with ``skip_locked`` ``None``
        when another runner holds it."""
        stmt = (
            select(IntegrationSyncItem)
            .where(
                IntegrationSyncItem.organization_id == organization_id,
                IntegrationSyncItem.id == item_id,
            )
            .with_for_update(skip_locked=skip_locked)
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def counts(self, organization_id: uuid.UUID) -> dict[str, dict[str, int]]:
        """``{integration: {status: count}}`` of the practice's items."""
        stmt = (
            select(IntegrationSyncItem.integration, IntegrationSyncItem.status, func.count())
            .where(IntegrationSyncItem.organization_id == organization_id)
            .group_by(IntegrationSyncItem.integration, IntegrationSyncItem.status)
        )
        found: dict[str, dict[str, int]] = {}
        for integration, status, count in (await self.session.execute(stmt)).all():
            found.setdefault(integration, {})[status] = int(count)
        return found

    async def failed_ids(
        self, organization_id: uuid.UUID, integration: str, *, limit: int
    ) -> list[uuid.UUID]:
        stmt = (
            select(IntegrationSyncItem.id)
            .where(
                IntegrationSyncItem.organization_id == organization_id,
                IntegrationSyncItem.integration == integration,
                IntegrationSyncItem.status == "failed",
            )
            .order_by(IntegrationSyncItem.created_at, IntegrationSyncItem.id)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())


class ConnectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(self, organization_id: uuid.UUID) -> dict[str, IntegrationConnection]:
        stmt = select(IntegrationConnection).where(
            IntegrationConnection.organization_id == organization_id
        )
        rows = (await self.session.execute(stmt)).scalars()
        return {row.integration: row for row in rows}

    async def get(
        self, organization_id: uuid.UUID, integration: str, *, lock: bool = False
    ) -> IntegrationConnection | None:
        stmt = select(IntegrationConnection).where(
            IntegrationConnection.organization_id == organization_id,
            IntegrationConnection.integration == integration,
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def ensure(
        self, organization_id: uuid.UUID, integration: str, *, enabled: bool, new_id: uuid.UUID
    ) -> IntegrationConnection:
        """The practice's connection row, created with ``enabled`` when it does not exist yet
        (two requests creating it at once leave one row), locked until the transaction ends."""
        await self.session.execute(
            insert(IntegrationConnection)
            .values(
                id=new_id, organization_id=organization_id, integration=integration, enabled=enabled
            )
            .on_conflict_do_nothing(index_elements=["organization_id", "integration"])
        )
        found = await self.get(organization_id, integration, lock=True)
        assert found is not None
        return found

    async def enabled(self, integration: str) -> list[IntegrationConnection]:
        """Practices' connections where the integration is switched on. Serves the worker."""
        stmt = select(IntegrationConnection).where(
            IntegrationConnection.integration == integration,
            IntegrationConnection.enabled.is_(True),
        )
        return list((await self.session.execute(stmt)).scalars())

    async def record_run(
        self,
        organization_id: uuid.UUID,
        integration: str,
        *,
        enabled_default: bool,
        succeeded: bool,
        error: str | None,
        at: datetime,
    ) -> None:
        """Note how a run went: when the integration last tried and last worked, and why it
        failed (one statement, so runs finishing together do not clash)."""
        message = (error or "The last attempt failed.")[:1000]
        values: dict[str, Any] = {
            "status": "ok" if succeeded else "error",
            "last_attempt_at": at,
            "last_error": None if succeeded else message,
        }
        if succeeded:
            values["last_success_at"] = at
        stmt = insert(IntegrationConnection).values(
            id=new_id(),
            organization_id=organization_id,
            integration=integration,
            enabled=enabled_default,
            **values,
        )
        await self.session.execute(
            stmt.on_conflict_do_update(
                index_elements=["organization_id", "integration"], set_=values
            )
        )
