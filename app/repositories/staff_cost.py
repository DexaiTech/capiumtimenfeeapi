"""Queries of the Staff Cost report (ticket APT81, phase D). Every query is scoped to one
organization; money sums are cast back to ``BIGINT`` so they arrive as Python ``int``."""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import BigInteger, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Client,
    Grade,
    Job,
    OrganizationMember,
    TimeEntry,
    TimeEntryOvertime,
    User,
)


@dataclass(frozen=True, slots=True)
class StaffCostScope:
    date_from: date
    date_to: date
    user_id: uuid.UUID | None = None
    grade_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class DayTime:
    """One person's time on one day."""

    user_id: uuid.UUID
    day: date
    minutes: int
    cost_value: int


@dataclass(frozen=True, slots=True)
class OvertimePiece:
    """One overtime row with what the report needs of its entry."""

    user_id: uuid.UUID
    day: date
    minutes: int
    allocated: bool
    overtime_rate: int
    overtime_cost: int
    status: str
    # The entry's standard cost rate: the standard cost of the overtime minutes.
    cost_rate: int
    job_id: uuid.UUID | None
    job_number: str | None
    job_name: str | None
    client_id: uuid.UUID | None
    client_name: str | None


@dataclass(frozen=True, slots=True)
class Person:
    name: str
    staff_code: str | None
    grade_name: str | None


class StaffCostRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _scoped(stmt: Any, organization_id: uuid.UUID, s: StaffCostScope) -> Any:
        stmt = stmt.join(
            OrganizationMember,
            and_(
                OrganizationMember.organization_id == TimeEntry.organization_id,
                OrganizationMember.user_id == TimeEntry.user_id,
            ),
        ).where(
            TimeEntry.organization_id == organization_id,
            TimeEntry.entry_date >= s.date_from,
            TimeEntry.entry_date <= s.date_to,
        )
        if s.user_id is not None:
            stmt = stmt.where(TimeEntry.user_id == s.user_id)
        if s.grade_id is not None:
            stmt = stmt.where(OrganizationMember.grade_id == s.grade_id)
        if s.job_id is not None:
            stmt = stmt.where(TimeEntry.job_id == s.job_id)
        if s.client_id is not None:
            stmt = stmt.where(TimeEntry.client_id == s.client_id)
        return stmt

    async def day_time(self, organization_id: uuid.UUID, s: StaffCostScope) -> list[DayTime]:
        """All the time in scope per person and day: minutes and standard cost."""
        stmt = self._scoped(
            select(
                TimeEntry.user_id,
                TimeEntry.entry_date,
                cast(func.sum(TimeEntry.minutes), BigInteger),
                cast(func.sum(TimeEntry.cost_value), BigInteger),
            ).select_from(TimeEntry),
            organization_id,
            s,
        ).group_by(TimeEntry.user_id, TimeEntry.entry_date)
        return [
            DayTime(row[0], row[1], int(row[2]), int(row[3]))
            for row in (await self.session.execute(stmt)).all()
        ]

    async def overtime(self, organization_id: uuid.UUID, s: StaffCostScope) -> list[OvertimePiece]:
        """The overtime rows of the time in scope, oldest entry first."""
        stmt = self._scoped(
            select(
                TimeEntryOvertime.user_id,
                TimeEntry.entry_date,
                TimeEntryOvertime.overtime_minutes,
                TimeEntryOvertime.allocated,
                TimeEntryOvertime.overtime_rate,
                TimeEntryOvertime.overtime_cost,
                TimeEntryOvertime.status,
                TimeEntry.cost_rate,
                TimeEntry.job_id,
                Job.number,
                Job.name,
                TimeEntry.client_id,
                Client.name,
            )
            .select_from(TimeEntryOvertime)
            .join(
                TimeEntry,
                and_(
                    TimeEntry.organization_id == TimeEntryOvertime.organization_id,
                    TimeEntry.id == TimeEntryOvertime.time_entry_id,
                ),
            ),
            organization_id,
            s,
        )
        stmt = (
            stmt.outerjoin(
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
            .order_by(TimeEntry.entry_date, TimeEntry.created_at, TimeEntry.id)
        )
        return [
            OvertimePiece(
                user_id=row[0],
                day=row[1],
                minutes=int(row[2]),
                allocated=bool(row[3]),
                overtime_rate=int(row[4]),
                overtime_cost=int(row[5]),
                status=row[6],
                cost_rate=int(row[7]),
                job_id=row[8],
                job_number=row[9],
                job_name=row[10],
                client_id=row[11],
                client_name=row[12],
            )
            for row in (await self.session.execute(stmt)).all()
        ]

    async def people(
        self, organization_id: uuid.UUID, user_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, Person]:
        if not user_ids:
            return {}
        stmt = (
            select(
                OrganizationMember.user_id,
                User.full_name,
                OrganizationMember.staff_code,
                Grade.name,
            )
            .join(User, User.id == OrganizationMember.user_id)
            .outerjoin(
                Grade,
                and_(
                    Grade.organization_id == OrganizationMember.organization_id,
                    Grade.id == OrganizationMember.grade_id,
                ),
            )
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id.in_(list(user_ids)),
            )
        )
        return {
            row[0]: Person(row[1] or "", row[2], row[3])
            for row in (await self.session.execute(stmt)).all()
        }

    async def grade_exists(self, organization_id: uuid.UUID, grade_id: uuid.UUID) -> bool:
        found = await self.session.scalar(
            select(Grade.id).where(Grade.organization_id == organization_id, Grade.id == grade_id)
        )
        return found is not None
