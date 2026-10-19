"""The log of timesheet reminders. Every method is scoped to one organization."""

import uuid
from collections.abc import Collection
from datetime import date, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TimesheetReminder
from app.utils.ids import new_id


class TimesheetReminderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim(
        self,
        organization_id: uuid.UUID,
        week_start: date,
        kind: str,
        user_ids: Collection[uuid.UUID],
    ) -> set[uuid.UUID]:
        """Record a reminder for each person who has none for the week yet and return those
        people. Race-safe: of two concurrent runs, each person is claimed by exactly one."""
        if not user_ids:
            return set()
        stmt = (
            insert(TimesheetReminder)
            .values(
                [
                    {
                        "id": new_id(),
                        "organization_id": organization_id,
                        "user_id": user_id,
                        "week_start": week_start,
                        "kind": kind,
                    }
                    for user_id in sorted(user_ids)
                ]
            )
            .on_conflict_do_nothing(
                index_elements=[
                    TimesheetReminder.organization_id,
                    TimesheetReminder.user_id,
                    TimesheetReminder.week_start,
                    TimesheetReminder.kind,
                ]
            )
            .returning(TimesheetReminder.user_id)
        )
        return set((await self.session.execute(stmt)).scalars())

    async def release(
        self,
        organization_id: uuid.UUID,
        week_start: date,
        kind: str,
        user_ids: Collection[uuid.UUID],
    ) -> None:
        """Take back claims whose reminder could not be raised, so a later run retries them."""
        if user_ids:
            await self.session.execute(
                delete(TimesheetReminder).where(
                    TimesheetReminder.organization_id == organization_id,
                    TimesheetReminder.week_start == week_start,
                    TimesheetReminder.kind == kind,
                    TimesheetReminder.user_id.in_(list(user_ids)),
                )
            )

    async def last_reminded(
        self,
        organization_id: uuid.UUID,
        week_start: date,
        kind: str,
        user_ids: Collection[uuid.UUID],
    ) -> dict[uuid.UUID, datetime]:
        if not user_ids:
            return {}
        stmt = (
            select(TimesheetReminder.user_id, func.max(TimesheetReminder.reminded_at))
            .where(
                TimesheetReminder.organization_id == organization_id,
                TimesheetReminder.week_start == week_start,
                TimesheetReminder.kind == kind,
                TimesheetReminder.user_id.in_(list(user_ids)),
            )
            .group_by(TimesheetReminder.user_id)
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}
