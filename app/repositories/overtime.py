"""Staff hours bases, overtime rates and the overtime of time entries (APT81). Every method is
scoped to one organization."""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Client,
    Grade,
    Job,
    OrganizationMember,
    OvertimeRate,
    StaffHoursBasis,
    TimeEntry,
    TimeEntryOvertime,
    Timesheet,
    User,
)
from app.repositories.time_paging import keyset_finish, keyset_query, parse_date
from app.utils.pagination import Page, PageParams


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

    async def add_default(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """A new member's working hours: one open-ended ``weekly`` row of their weekly capacity,
        from their start date, else the day they joined (as migration 081 gave everyone). None
        with a capacity of 0, or when they already have hours."""
        await self.session.execute(
            text(
                """
                INSERT INTO staff_hours_bases (
                    id, organization_id, user_id, basis, standard_minutes, effective_from
                )
                SELECT gen_random_uuid(), m.organization_id, m.user_id, 'weekly',
                       m.weekly_capacity_minutes,
                       coalesce(m.start_date, (m.joined_at AT TIME ZONE o.timezone)::date)
                FROM organization_members m
                JOIN organizations o ON o.id = m.organization_id
                WHERE m.organization_id = :org AND m.user_id = :user
                  AND m.weekly_capacity_minutes > 0
                  AND NOT EXISTS (
                    SELECT 1 FROM staff_hours_bases h
                    WHERE h.organization_id = m.organization_id AND h.user_id = m.user_id)
                """
            ),
            {"org": organization_id, "user": user_id},
        )

    async def for_users(
        self, organization_id: uuid.UUID, user_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, list[StaffHoursBasis]]:
        """Each person's hours, oldest first."""
        found: dict[uuid.UUID, list[StaffHoursBasis]] = {u: [] for u in user_ids}
        if not user_ids:
            return found
        stmt = (
            select(StaffHoursBasis)
            .where(
                StaffHoursBasis.organization_id == organization_id,
                StaffHoursBasis.user_id.in_(list(user_ids)),
            )
            .order_by(StaffHoursBasis.user_id, StaffHoursBasis.effective_from)
        )
        for row in (await self.session.execute(stmt)).scalars():
            found[row.user_id].append(row)
        return found

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


# --- The overtime of time entries ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PeriodEntry:
    """A time entry of a person's overtime period, with what decides its overtime row."""

    entry: TimeEntry
    # The entry's timesheet needs no approval (the practice does not approve time, or the
    # person is exempt): its overtime is approved as it is recorded.
    approval_exempt: bool
    # On a job of a client other than the built-in Internal client.
    allocated: bool


@dataclass(frozen=True, slots=True)
class OvertimeView:
    """An overtime row with its entry and the names a list shows."""

    row: TimeEntryOvertime
    entry_date: date
    entry_minutes: int
    description: str | None
    user_name: str | None
    job_id: uuid.UUID | None
    job_number: str | None
    job_name: str | None
    client_id: uuid.UUID | None
    client_name: str | None
    decided_by_name: str | None


@dataclass(frozen=True, slots=True)
class OvertimeFilters:
    # Null: everyone's.
    user_ids: Collection[uuid.UUID] | None = None
    status: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    allocated: bool | None = None
    overtime_id: uuid.UUID | None = None


class TimeOvertimeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock_person(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Serialise the overtime of one person's periods (until the transaction ends)."""
        await _lock(self.session, f"overtime:{organization_id}:{user_id}")

    async def period_entries(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, start: date, end: date
    ) -> list[PeriodEntry]:
        """The person's entries from ``start`` to ``end``, in the order overtime counts them:
        entry date, then the time recorded, then id."""
        stmt = (
            select(TimeEntry, Timesheet.approval_exempt, Client.is_internal)
            .join(
                Timesheet,
                and_(
                    Timesheet.organization_id == TimeEntry.organization_id,
                    Timesheet.id == TimeEntry.timesheet_id,
                ),
            )
            .outerjoin(
                Client,
                and_(
                    Client.organization_id == TimeEntry.organization_id,
                    Client.id == TimeEntry.client_id,
                ),
            )
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.user_id == user_id,
                TimeEntry.entry_date >= start,
                TimeEntry.entry_date <= end,
            )
            .order_by(TimeEntry.entry_date, TimeEntry.created_at, TimeEntry.id)
        )
        return [
            PeriodEntry(entry, bool(exempt), entry.job_id is not None and internal is False)
            for entry, exempt, internal in (await self.session.execute(stmt)).all()
        ]

    async def for_entries(
        self, organization_id: uuid.UUID, entry_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, TimeEntryOvertime]:
        if not entry_ids:
            return {}
        stmt = (
            select(TimeEntryOvertime)
            .where(
                TimeEntryOvertime.organization_id == organization_id,
                TimeEntryOvertime.time_entry_id.in_(list(entry_ids)),
            )
            .execution_options(populate_existing=True)
        )
        return {row.time_entry_id: row for row in (await self.session.execute(stmt)).scalars()}

    async def add(self, row: TimeEntryOvertime) -> None:
        self.session.add(row)
        await self.session.flush()

    async def delete(self, row: TimeEntryOvertime) -> None:
        await self.session.delete(row)
        await self.session.flush()

    async def get(
        self, organization_id: uuid.UUID, overtime_id: uuid.UUID, *, lock: bool = False
    ) -> TimeEntryOvertime | None:
        stmt = select(TimeEntryOvertime).where(
            TimeEntryOvertime.organization_id == organization_id,
            TimeEntryOvertime.id == overtime_id,
        )
        if lock:
            stmt = stmt.with_for_update()
        stmt = stmt.execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def views_page(
        self, organization_id: uuid.UUID, f: OvertimeFilters, params: PageParams
    ) -> Page[OvertimeView]:
        """One page of overtime rows with their entries, oldest entry first (keyset on the
        entry date and the overtime id)."""
        stmt = keyset_query(
            self._views_select(organization_id, f),
            sort_expr=TimeEntry.entry_date,
            id_column=TimeEntryOvertime.id,
            params=params,
            descending=False,
            parse=parse_date,
        )
        views = [OvertimeView(*row) for row in (await self.session.execute(stmt)).all()]
        items, next_cursor = keyset_finish(views, params, lambda v: (v.entry_date, v.row.id))
        return Page(items=items, next_cursor=next_cursor)

    async def view(self, organization_id: uuid.UUID, overtime_id: uuid.UUID) -> OvertimeView:
        stmt = self._views_select(organization_id, OvertimeFilters(overtime_id=overtime_id))
        [row] = (await self.session.execute(stmt)).all()
        return OvertimeView(*row)

    @staticmethod
    def _views_select(organization_id: uuid.UUID, f: OvertimeFilters) -> Any:
        decider = User.__table__.alias("decider")
        stmt = (
            select(
                TimeEntryOvertime,
                TimeEntry.entry_date,
                TimeEntry.minutes,
                TimeEntry.description,
                User.full_name,
                TimeEntry.job_id,
                Job.number,
                Job.name,
                TimeEntry.client_id,
                Client.name,
                decider.c.full_name,
            )
            .join(
                TimeEntry,
                and_(
                    TimeEntry.organization_id == TimeEntryOvertime.organization_id,
                    TimeEntry.id == TimeEntryOvertime.time_entry_id,
                ),
            )
            .join(User, User.id == TimeEntryOvertime.user_id)
            .outerjoin(
                Job,
                and_(Job.organization_id == TimeEntry.organization_id, Job.id == TimeEntry.job_id),
            )
            .outerjoin(
                Client,
                and_(
                    Client.organization_id == TimeEntry.organization_id,
                    Client.id == TimeEntry.client_id,
                ),
            )
            .outerjoin(decider, decider.c.id == TimeEntryOvertime.decided_by)
            .where(TimeEntryOvertime.organization_id == organization_id)
            .execution_options(populate_existing=True)
        )
        if f.user_ids is not None:
            stmt = stmt.where(TimeEntryOvertime.user_id.in_(list(f.user_ids)))
        if f.status is not None:
            stmt = stmt.where(TimeEntryOvertime.status == f.status)
        if f.date_from is not None:
            stmt = stmt.where(TimeEntry.entry_date >= f.date_from)
        if f.date_to is not None:
            stmt = stmt.where(TimeEntry.entry_date <= f.date_to)
        if f.allocated is not None:
            stmt = stmt.where(TimeEntryOvertime.allocated.is_(f.allocated))
        if f.overtime_id is not None:
            stmt = stmt.where(TimeEntryOvertime.id == f.overtime_id)
        return stmt
