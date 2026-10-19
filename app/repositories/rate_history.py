"""Rate change history: appends and the filtered, ordered list. Scoped to one organization."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db.models import Client, Grade, RateChange, Service, User
from app.repositories.time_paging import keyset_finish, keyset_query
from app.utils.pagination import Page, PageParams


@dataclass(frozen=True, slots=True)
class ChangeView:
    change: RateChange
    client_name: str | None
    service_name: str | None
    user_name: str | None
    grade_name: str | None
    actor_name: str | None


@dataclass(frozen=True, slots=True)
class ChangeFilters:
    kind: str | None = None
    action: str | None = None
    user_id: uuid.UUID | None = None
    grade_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    # ``created_at >= from_at`` and ``created_at < to_at``.
    from_at: datetime | None = None
    to_at: datetime | None = None


class RateHistoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, change: RateChange) -> None:
        self.session.add(change)
        await self.session.flush()

    async def list_page(
        self, organization_id: uuid.UUID, filters: ChangeFilters, params: PageParams
    ) -> Page[ChangeView]:
        """Newest first by ``(created_at, id)``: every change, in the order it happened."""
        actor = aliased(User)
        subject = aliased(User)
        stmt: Any = (
            select(
                RateChange,
                Client.name,
                Service.name,
                subject.full_name,
                Grade.name,
                actor.full_name,
            )
            .outerjoin(
                Client,
                and_(
                    Client.organization_id == RateChange.organization_id,
                    Client.id == RateChange.client_id,
                ),
            )
            .outerjoin(
                Service,
                and_(
                    Service.organization_id == RateChange.organization_id,
                    Service.id == RateChange.service_id,
                ),
            )
            .outerjoin(subject, subject.id == RateChange.user_id)
            .outerjoin(
                Grade,
                and_(
                    Grade.organization_id == RateChange.organization_id,
                    Grade.id == RateChange.grade_id,
                ),
            )
            .outerjoin(actor, actor.id == RateChange.actor_user_id)
            .where(RateChange.organization_id == organization_id)
            .execution_options(populate_existing=True)
        )
        if filters.kind is not None:
            stmt = stmt.where(RateChange.kind == filters.kind)
        if filters.action is not None:
            stmt = stmt.where(RateChange.action == filters.action)
        if filters.user_id is not None:
            stmt = stmt.where(RateChange.user_id == filters.user_id)
        if filters.grade_id is not None:
            stmt = stmt.where(RateChange.grade_id == filters.grade_id)
        if filters.client_id is not None:
            stmt = stmt.where(RateChange.client_id == filters.client_id)
        if filters.service_id is not None:
            stmt = stmt.where(RateChange.service_id == filters.service_id)
        if filters.from_at is not None:
            stmt = stmt.where(RateChange.created_at >= filters.from_at)
        if filters.to_at is not None:
            stmt = stmt.where(RateChange.created_at < filters.to_at)
        stmt = keyset_query(
            stmt,
            sort_expr=RateChange.created_at,
            id_column=RateChange.id,
            params=params,
            descending=True,
            parse=lambda raw: datetime.fromisoformat(str(raw)),
        )
        rows = [ChangeView(*row) for row in (await self.session.execute(stmt)).all()]
        items, next_cursor = keyset_finish(
            rows, params, lambda v: (v.change.created_at, v.change.id)
        )
        return Page(items=items, next_cursor=next_cursor)
