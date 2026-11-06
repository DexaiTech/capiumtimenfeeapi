"""Staff hours bases and overtime rates (APT81). Every method is scoped to one organization."""

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Grade, OrganizationMember, OvertimeRate, StaffHoursBasis, User


def _covers(
    model: type[StaffHoursBasis] | type[OvertimeRate], start: date, end: date | None
) -> ColumnElement[bool]:
    """The row's period shares a day with ``[start, end]`` (open-ended when null)."""
    conditions = [or_(model.effective_to.is_(None), model.effective_to >= start)]
    if end is not None:
        conditions.append(model.effective_from <= end)
    return and_(*conditions)


async def _lock(session: AsyncSession, key: str) -> None:
    """Serialise changes to one person's or one grade's periods (until the transaction ends)."""
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


class StaffHoursRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for(
        self, organization_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[StaffHoursBasis]:
        stmt = (
            select(StaffHoursBasis)
            .where(
                StaffHoursBasis.organization_id == organization_id,
                StaffHoursBasis.user_id == user_id,
            )
            .order_by(StaffHoursBasis.effective_from.desc())
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, hours_id: uuid.UUID
    ) -> StaffHoursBasis | None:
        stmt = select(StaffHoursBasis).where(
            StaffHoursBasis.organization_id == organization_id,
            StaffHoursBasis.user_id == user_id,
            StaffHoursBasis.id == hours_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def overlapping(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, start: date, end: date | None
    ) -> list[StaffHoursBasis]:
        stmt = (
            select(StaffHoursBasis)
            .where(
                StaffHoursBasis.organization_id == organization_id,
                StaffHoursBasis.user_id == user_id,
                _covers(StaffHoursBasis, start, end),
            )
            .order_by(StaffHoursBasis.effective_from)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def lock_person(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> None:
        await _lock(self.session, f"staff_hours:{organization_id}:{user_id}")

    async def add(self, row: StaffHoursBasis) -> StaffHoursBasis:
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return row

    async def save(self, row: StaffHoursBasis) -> StaffHoursBasis:
        await self.session.flush()
        await self.session.refresh(row)
        return row

    async def delete(self, row: StaffHoursBasis) -> None:
        await self.session.execute(
            delete(StaffHoursBasis).where(
                StaffHoursBasis.organization_id == row.organization_id,
                StaffHoursBasis.id == row.id,
            )
        )


@dataclass(frozen=True, slots=True)
class OvertimeRateView:
    """A rate with the names a list shows."""

    rate: OvertimeRate
    grade_code: str | None
    grade_name: str | None
    staff_code: str | None
    user_name: str | None


@dataclass(frozen=True, slots=True)
class OvertimeRateFilters:
    level: str | None = None
    user_id: uuid.UUID | None = None
    grade_id: uuid.UUID | None = None
    # ``current``, ``future`` or ``ended`` relative to ``today``.
    status: str | None = None
    today: date | None = None
    # Only rates in force on this day.
    on_date: date | None = None


def _view_select() -> Any:
    return (
        select(OvertimeRate, Grade.code, Grade.name, OrganizationMember.staff_code, User.full_name)
        .outerjoin(
            Grade,
            and_(
                Grade.organization_id == OvertimeRate.organization_id,
                Grade.id == OvertimeRate.grade_id,
            ),
        )
        .outerjoin(
            OrganizationMember,
            and_(
                OrganizationMember.organization_id == OvertimeRate.organization_id,
                OrganizationMember.user_id == OvertimeRate.user_id,
            ),
        )
        .outerjoin(User, User.id == OvertimeRate.user_id)
        .execution_options(populate_existing=True)
    )


class OvertimeRateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self, organization_id: uuid.UUID, rate_id: uuid.UUID, *, lock: bool = False
    ) -> OvertimeRate | None:
        stmt = select(OvertimeRate).where(
            OvertimeRate.organization_id == organization_id, OvertimeRate.id == rate_id
        )
        if lock:
            stmt = stmt.with_for_update()
        stmt = stmt.execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def view(self, organization_id: uuid.UUID, rate_id: uuid.UUID) -> OvertimeRateView | None:
        stmt = _view_select().where(
            OvertimeRate.organization_id == organization_id, OvertimeRate.id == rate_id
        )
        row = (await self.session.execute(stmt)).first()
        return OvertimeRateView(*row) if row else None

    async def list_all(
        self, organization_id: uuid.UUID, f: OvertimeRateFilters
    ) -> list[OvertimeRateView]:
        """Grade rates, then staff rates; newest period first within each grade or person."""
        stmt = _view_select().where(OvertimeRate.organization_id == organization_id)
        if f.level is not None:
            stmt = stmt.where(OvertimeRate.level == f.level)
        if f.user_id is not None:
            stmt = stmt.where(OvertimeRate.user_id == f.user_id)
        if f.grade_id is not None:
            stmt = stmt.where(OvertimeRate.grade_id == f.grade_id)
        if f.on_date is not None:
            stmt = stmt.where(_covers(OvertimeRate, f.on_date, f.on_date))
        if f.status is not None and f.today is not None:
            if f.status == "current":
                stmt = stmt.where(_covers(OvertimeRate, f.today, f.today))
            elif f.status == "future":
                stmt = stmt.where(OvertimeRate.effective_from > f.today)
            elif f.status == "ended":
                stmt = stmt.where(OvertimeRate.effective_to < f.today)
        stmt = stmt.order_by(
            OvertimeRate.level,
            Grade.name,
            User.full_name,
            OvertimeRate.effective_from.desc(),
            OvertimeRate.id,
        )
        return [OvertimeRateView(*row) for row in (await self.session.execute(stmt)).all()]

    async def overlapping(
        self,
        organization_id: uuid.UUID,
        *,
        user_id: uuid.UUID | None,
        grade_id: uuid.UUID | None,
        start: date,
        end: date | None,
        exclude_id: uuid.UUID | None = None,
    ) -> list[OvertimeRate]:
        """Rates of the same grade or person whose period shares a day with ``[start, end]``."""
        stmt = select(OvertimeRate).where(
            OvertimeRate.organization_id == organization_id,
            OvertimeRate.user_id.is_(None) if user_id is None else OvertimeRate.user_id == user_id,
            OvertimeRate.grade_id.is_(None)
            if grade_id is None
            else OvertimeRate.grade_id == grade_id,
            _covers(OvertimeRate, start, end),
        )
        if exclude_id is not None:
            stmt = stmt.where(OvertimeRate.id != exclude_id)
        return list(
            (await self.session.execute(stmt.order_by(OvertimeRate.effective_from))).scalars()
        )

    async def lock_scope(
        self, organization_id: uuid.UUID, user_id: uuid.UUID | None, grade_id: uuid.UUID | None
    ) -> None:
        await _lock(self.session, f"overtime_rate:{organization_id}:{user_id}:{grade_id}")

    async def add(self, rate: OvertimeRate) -> OvertimeRate:
        self.session.add(rate)
        await self.session.flush()
        await self.session.refresh(rate)
        return rate

    async def save(self, rate: OvertimeRate) -> OvertimeRate:
        await self.session.flush()
        await self.session.refresh(rate)
        return rate

    async def delete(self, organization_id: uuid.UUID, rate_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(OvertimeRate).where(
                OvertimeRate.organization_id == organization_id, OvertimeRate.id == rate_id
            )
        )
