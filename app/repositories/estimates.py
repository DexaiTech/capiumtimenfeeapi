"""Estimate and estimate line queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, delete, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, Estimate, EstimateLine
from app.utils.pagination import Page, PageParams, paginate

DISPLAY_STATUSES = ("draft", "sent", "accepted", "declined", "expired")


@dataclass(frozen=True, slots=True)
class EstimateFilters:
    client_id: uuid.UUID | None = None
    statuses: Sequence[str] | None = None
    q: str | None = None


@dataclass(frozen=True, slots=True)
class EstimateFigures:
    total: int
    by_status: dict[str, int]
    open_gross: int


def _open(today: date) -> ColumnElement[bool]:
    """Sent, and not past its expiry date."""
    return and_(
        Estimate.status == "sent",
        or_(Estimate.expiry_date.is_(None), Estimate.expiry_date >= today),
    )


def display_status_condition(status: str, today: date) -> ColumnElement[bool]:
    """SQL for the derived display status (``expired`` is a sent estimate past its expiry)."""
    match status:
        case "draft" | "accepted" | "declined":
            return Estimate.status == status
        case "sent":
            return _open(today)
        case "expired":
            return and_(Estimate.status == "sent", Estimate.expiry_date < today)
    return false()


def _apply_common_filters(
    stmt: Select[*tuple[Any, ...]], filters: EstimateFilters
) -> Select[*tuple[Any, ...]]:
    if filters.client_id is not None:
        stmt = stmt.where(Estimate.client_id == filters.client_id)
    if filters.q:
        client_match = (
            select(Client.id)
            .where(
                Client.organization_id == Estimate.organization_id,
                Client.id == Estimate.client_id,
                or_(
                    Client.name.icontains(filters.q, autoescape=True),
                    Client.code.icontains(filters.q, autoescape=True),
                ),
            )
            .exists()
        )
        stmt = stmt.where(
            or_(
                Estimate.number.icontains(filters.q, autoescape=True),
                Estimate.reference.icontains(filters.q, autoescape=True),
                client_match,
            )
        )
    return stmt


class EstimateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, estimate: Estimate) -> Estimate:
        self.session.add(estimate)
        await self.session.flush()
        await self.session.refresh(estimate)
        return estimate

    async def get(
        self, organization_id: uuid.UUID, estimate_id: uuid.UUID, *, for_update: bool = False
    ) -> Estimate | None:
        stmt = select(Estimate).where(
            Estimate.organization_id == organization_id, Estimate.id == estimate_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def delete(self, estimate: Estimate) -> None:
        await self.session.delete(estimate)
        await self.session.flush()

    async def lines(self, organization_id: uuid.UUID, estimate_id: uuid.UUID) -> list[EstimateLine]:
        stmt = (
            select(EstimateLine)
            .where(
                EstimateLine.organization_id == organization_id,
                EstimateLine.estimate_id == estimate_id,
            )
            .order_by(EstimateLine.position, EstimateLine.id)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def replace_lines(
        self, organization_id: uuid.UUID, estimate_id: uuid.UUID, lines: Sequence[EstimateLine]
    ) -> None:
        await self.session.execute(
            delete(EstimateLine).where(
                EstimateLine.organization_id == organization_id,
                EstimateLine.estimate_id == estimate_id,
            )
        )
        self.session.add_all(lines)
        await self.session.flush()

    async def list_page(
        self,
        organization_id: uuid.UUID,
        filters: EstimateFilters,
        *,
        today: date,
        params: PageParams,
    ) -> Page[Estimate]:
        """Newest first by ``(created_at, id)``."""
        stmt = select(Estimate).where(Estimate.organization_id == organization_id)
        stmt = _apply_common_filters(stmt, filters)
        if filters.statuses:
            stmt = stmt.where(or_(*(display_status_condition(s, today) for s in filters.statuses)))
        return await paginate(
            self.session,
            stmt,
            sort_column=Estimate.created_at,
            id_column=Estimate.id,
            params=params,
            descending=True,
        )

    async def summary(
        self, organization_id: uuid.UUID, filters: EstimateFilters, *, today: date
    ) -> EstimateFigures:
        """Counts per display status and the value still open, for the client and search
        filters (the status filter is ignored)."""
        stmt = select(
            func.count(),
            *(
                func.count().filter(display_status_condition(status, today))
                for status in DISPLAY_STATUSES
            ),
            func.coalesce(func.sum(Estimate.gross_total).filter(_open(today)), 0),
        ).where(Estimate.organization_id == organization_id)
        stmt = _apply_common_filters(stmt, filters)
        row = (await self.session.execute(stmt)).one()
        return EstimateFigures(
            total=int(row[0]),
            by_status={s: int(row[i + 1]) for i, s in enumerate(DISPLAY_STATUSES)},
            open_gross=int(row[len(DISPLAY_STATUSES) + 1]),
        )
