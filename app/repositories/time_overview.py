"""Aggregates behind the day and calendar views and the missing-timesheet list.

Every method is scoped to one organization.
"""

import uuid
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OrganizationMember, TimeEntry, Timesheet, User


@dataclass(frozen=True, slots=True)
class DayFigures:
    entry_count: int
    total_minutes: int
    billable_minutes: int


class TimeOverviewRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def day_figures(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, date_from: date, date_to: date
    ) -> dict[date, DayFigures]:
        """Entries, minutes and billable minutes of the person on each day that has time."""
        stmt = (
            select(
                TimeEntry.entry_date,
                func.count(),
                func.coalesce(func.sum(TimeEntry.minutes), 0),
                func.coalesce(
                    func.sum(case((TimeEntry.is_billable, TimeEntry.minutes), else_=0)), 0
                ),
            )
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.user_id == user_id,
                TimeEntry.entry_date >= date_from,
                TimeEntry.entry_date <= date_to,
            )
            .group_by(TimeEntry.entry_date)
        )
        return {
            row[0]: DayFigures(int(row[1]), int(row[2]), int(row[3]))
            for row in (await self.session.execute(stmt)).all()
        }

    async def sheets_by_week(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, first_week: date, last_day: date
    ) -> dict[date, Timesheet]:
        """The person's sheets whose week starts in ``[first_week, last_day]``, by week start."""
        stmt = select(Timesheet).where(
            Timesheet.organization_id == organization_id,
            Timesheet.user_id == user_id,
            Timesheet.week_start >= first_week,
            Timesheet.week_start <= last_day,
        )
        return {s.week_start: s for s in (await self.session.execute(stmt)).scalars()}

    async def staff(
        self,
        organization_id: uuid.UUID,
        *,
        week_start: date,
        week_end: date,
        user_ids: Collection[uuid.UUID] | None,
        manager_user_id: uuid.UUID | None,
    ) -> list[tuple[OrganizationMember, str]]:
        """Active staff with a weekly capacity who are employed at some point of the week,
        by name. ``user_ids`` / ``manager_user_id`` narrow it to some people."""
        stmt = (
            select(OrganizationMember, User.full_name)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.status == "active",
                OrganizationMember.weekly_capacity_minutes > 0,
                or_(
                    OrganizationMember.start_date.is_(None),
                    OrganizationMember.start_date <= week_end,
                ),
                or_(
                    OrganizationMember.leaving_date.is_(None),
                    OrganizationMember.leaving_date >= week_start,
                ),
            )
            .order_by(func.lower(User.full_name), OrganizationMember.user_id)
        )
        if user_ids is not None:
            stmt = stmt.where(OrganizationMember.user_id.in_(list(user_ids)))
        if manager_user_id is not None:
            stmt = stmt.where(OrganizationMember.manager_user_id == manager_user_id)
        return [(row[0], row[1]) for row in (await self.session.execute(stmt)).all()]

    async def sheets_of_week(
        self, organization_id: uuid.UUID, week_start: date, user_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, Timesheet]:
        """The sheets of ``week_start`` of these people, by person."""
        if not user_ids:
            return {}
        stmt = select(Timesheet).where(
            Timesheet.organization_id == organization_id,
            Timesheet.week_start == week_start,
            Timesheet.user_id.in_(list(user_ids)),
        )
        return {s.user_id: s for s in (await self.session.execute(stmt)).scalars()}

    async def minutes_by_day(
        self,
        organization_id: uuid.UUID,
        user_ids: Collection[uuid.UUID],
        date_from: date,
        date_to: date,
    ) -> dict[uuid.UUID, dict[date, int]]:
        """Minutes logged by each person on each day of the range."""
        if not user_ids:
            return {}
        stmt = (
            select(TimeEntry.user_id, TimeEntry.entry_date, func.sum(TimeEntry.minutes))
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.user_id.in_(list(user_ids)),
                TimeEntry.entry_date >= date_from,
                TimeEntry.entry_date <= date_to,
            )
            .group_by(TimeEntry.user_id, TimeEntry.entry_date)
        )
        found: dict[uuid.UUID, dict[date, int]] = defaultdict(dict)
        for user_id, day, minutes in (await self.session.execute(stmt)).all():
            found[user_id][day] = int(minutes)
        return dict(found)

    async def waiting_sheets(
        self,
        organization_id: uuid.UUID,
        up_to_week: date,
        *,
        user_ids: Collection[uuid.UUID] | None,
        manager_user_id: uuid.UUID | None,
    ) -> list[Timesheet]:
        """Submitted sheets of ``up_to_week`` or an earlier week, oldest first."""
        stmt = select(Timesheet).where(
            Timesheet.organization_id == organization_id,
            Timesheet.status == "submitted",
            Timesheet.week_start <= up_to_week,
        )
        if user_ids is not None:
            stmt = stmt.where(Timesheet.user_id.in_(list(user_ids)))
        if manager_user_id is not None:
            stmt = stmt.where(
                Timesheet.user_id.in_(
                    select(OrganizationMember.user_id).where(
                        OrganizationMember.organization_id == organization_id,
                        OrganizationMember.manager_user_id == manager_user_id,
                    )
                )
            )
        return list(
            (
                await self.session.execute(stmt.order_by(Timesheet.week_start, Timesheet.id))
            ).scalars()
        )
