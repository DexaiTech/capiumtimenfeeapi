"""Recurring schedule, template line and run queries. Scoped to one organization."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, Invoice, RecurringLine, RecurringRun, RecurringSchedule
from app.utils.money import line_net
from app.utils.pagination import Page, PageParams, paginate


@dataclass(frozen=True, slots=True)
class ScheduleFilters:
    client_id: uuid.UUID | None = None
    statuses: Sequence[str] | None = None
    q: str | None = None


class RecurringRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Schedules ---------------------------------------------------------------------

    async def add(self, schedule: RecurringSchedule) -> RecurringSchedule:
        self.session.add(schedule)
        await self.session.flush()
        await self.session.refresh(schedule)
        return schedule

    async def get(
        self,
        organization_id: uuid.UUID,
        schedule_id: uuid.UUID,
        *,
        for_update: bool = False,
        skip_locked: bool = False,
    ) -> RecurringSchedule | None:
        stmt = select(RecurringSchedule).where(
            RecurringSchedule.organization_id == organization_id,
            RecurringSchedule.id == schedule_id,
        )
        if for_update:
            stmt = stmt.with_for_update(skip_locked=skip_locked)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def delete(self, schedule: RecurringSchedule) -> None:
        await self.session.delete(schedule)
        await self.session.flush()

    async def list_page(
        self, organization_id: uuid.UUID, filters: ScheduleFilters, params: PageParams
    ) -> Page[RecurringSchedule]:
        """Newest first by ``(created_at, id)``."""
        stmt = select(RecurringSchedule).where(RecurringSchedule.organization_id == organization_id)
        if filters.client_id is not None:
            stmt = stmt.where(RecurringSchedule.client_id == filters.client_id)
        if filters.statuses:
            stmt = stmt.where(RecurringSchedule.status.in_(filters.statuses))
        if filters.q:
            client_match = (
                select(Client.id)
                .where(
                    Client.organization_id == RecurringSchedule.organization_id,
                    Client.id == RecurringSchedule.client_id,
                    or_(
                        Client.name.icontains(filters.q, autoescape=True),
                        Client.code.icontains(filters.q, autoescape=True),
                    ),
                )
                .exists()
            )
            stmt = stmt.where(
                or_(RecurringSchedule.name.icontains(filters.q, autoescape=True), client_match)
            )
        return await paginate(
            self.session,
            stmt,
            sort_column=RecurringSchedule.created_at,
            id_column=RecurringSchedule.id,
            params=params,
            descending=True,
        )

    async def due_organizations(self, up_to: date) -> list[uuid.UUID]:
        """Practices with an active schedule that may be due (``up_to`` is a day ahead of UTC
        today, so every time zone is covered; each practice re-checks in its own)."""
        stmt = (
            select(RecurringSchedule.organization_id)
            .where(RecurringSchedule.status == "active", RecurringSchedule.next_run_date <= up_to)
            .distinct()
            .order_by(RecurringSchedule.organization_id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def due_schedule_ids(self, organization_id: uuid.UUID, today: date) -> list[uuid.UUID]:
        """Active schedules due today or earlier that have no run row for that date yet (a
        failed run waits for its manual retry)."""
        ran = exists().where(
            RecurringRun.schedule_id == RecurringSchedule.id,
            RecurringRun.run_date == RecurringSchedule.next_run_date,
        )
        stmt = (
            select(RecurringSchedule.id)
            .where(
                RecurringSchedule.organization_id == organization_id,
                RecurringSchedule.status == "active",
                RecurringSchedule.next_run_date <= today,
                ~ran,
            )
            .order_by(RecurringSchedule.next_run_date, RecurringSchedule.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    # --- Template lines ----------------------------------------------------------------

    async def lines(
        self, organization_id: uuid.UUID, schedule_id: uuid.UUID
    ) -> list[RecurringLine]:
        stmt = (
            select(RecurringLine)
            .where(
                RecurringLine.organization_id == organization_id,
                RecurringLine.schedule_id == schedule_id,
            )
            .order_by(RecurringLine.position, RecurringLine.id)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def replace_lines(
        self, organization_id: uuid.UUID, schedule_id: uuid.UUID, lines: Sequence[RecurringLine]
    ) -> None:
        await self.session.execute(
            delete(RecurringLine).where(
                RecurringLine.organization_id == organization_id,
                RecurringLine.schedule_id == schedule_id,
            )
        )
        self.session.add_all(lines)
        await self.session.flush()

    async def net_estimates(
        self, organization_id: uuid.UUID, schedule_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Net of one invoice per schedule (before the VAT of the day)."""
        if not schedule_ids:
            return {}
        stmt = select(RecurringLine).where(
            RecurringLine.organization_id == organization_id,
            RecurringLine.schedule_id.in_(schedule_ids),
        )
        totals: dict[uuid.UUID, int] = {}
        for line in (await self.session.execute(stmt)).scalars():
            totals[line.schedule_id] = totals.get(line.schedule_id, 0) + line_net(
                line.quantity, line.unit_amount
            )
        return totals

    # --- Runs --------------------------------------------------------------------------

    async def add_run(self, run: RecurringRun) -> RecurringRun:
        self.session.add(run)
        await self.session.flush()
        await self.session.refresh(run)
        return run

    async def get_run(self, organization_id: uuid.UUID, run_id: uuid.UUID) -> RecurringRun | None:
        stmt = (
            select(RecurringRun)
            .where(RecurringRun.organization_id == organization_id, RecurringRun.id == run_id)
            .with_for_update()
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def run_exists(self, schedule_id: uuid.UUID, run_date: date) -> bool:
        stmt = select(
            exists().where(
                RecurringRun.schedule_id == schedule_id, RecurringRun.run_date == run_date
            )
        )
        return bool((await self.session.execute(stmt)).scalar_one())

    async def runs_page(
        self, organization_id: uuid.UUID, schedule_id: uuid.UUID, params: PageParams
    ) -> Page[RecurringRun]:
        """Newest first by ``(run_date, id)``."""
        stmt = select(RecurringRun).where(
            RecurringRun.organization_id == organization_id,
            RecurringRun.schedule_id == schedule_id,
        )
        return await paginate(
            self.session,
            stmt,
            sort_column=RecurringRun.run_date,
            id_column=RecurringRun.id,
            params=params,
            descending=True,
        )

    async def failed_counts(
        self, organization_id: uuid.UUID, schedule_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        if not schedule_ids:
            return {}
        stmt = (
            select(RecurringRun.schedule_id, func.count())
            .where(
                RecurringRun.organization_id == organization_id,
                RecurringRun.schedule_id.in_(schedule_ids),
                RecurringRun.status == "failed",
            )
            .group_by(RecurringRun.schedule_id)
        )
        return {row[0]: int(row[1]) for row in await self.session.execute(stmt)}

    async def invoice_numbers(
        self, organization_id: uuid.UUID, invoice_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, str | None]:
        ids = [i for i in invoice_ids if i]
        if not ids:
            return {}
        stmt = select(Invoice.id, Invoice.number).where(
            Invoice.organization_id == organization_id, Invoice.id.in_(ids)
        )
        return {row[0]: row[1] for row in await self.session.execute(stmt)}

    async def has_runs(self, schedule_id: uuid.UUID) -> bool:
        stmt = select(exists().where(RecurringRun.schedule_id == schedule_id))
        return bool((await self.session.execute(stmt)).scalar_one())


__all__ = ["RecurringRepository", "ScheduleFilters"]
