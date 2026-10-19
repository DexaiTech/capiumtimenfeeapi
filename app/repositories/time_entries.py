"""Time entry and timer queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, delete, exists, func, select, true
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ActivityCode, Client, Job, Service, Task, TimeEntry, Timer, Timesheet
from app.repositories.time_paging import keyset_finish, keyset_query, parse_date
from app.utils.pagination import Page, PageParams


def wip_time_clause(require_timesheet_approval: bool) -> ColumnElement[bool]:
    """The time entries that are billable WIP (docs/domain.md, "Timesheets" and "WIP").

    Billable, ``unbilled`` and, when the practice requires approval, on an ``approved``
    timesheet. Self-contained (an ``EXISTS`` on the sheet), so it can be added to any query
    over ``time_entries`` without a join.
    """
    clause = and_(TimeEntry.is_billable.is_(True), TimeEntry.billing_status == "unbilled")
    if require_timesheet_approval:
        approved = exists().where(
            Timesheet.organization_id == TimeEntry.organization_id,
            Timesheet.id == TimeEntry.timesheet_id,
            Timesheet.status == "approved",
        )
        clause = and_(clause, approved)
    return clause


@dataclass(frozen=True, slots=True)
class EntryView:
    """A time entry with its sheet status and the names a list shows."""

    entry: TimeEntry
    timesheet_status: str
    client_code: str | None
    client_name: str | None
    job_number: str | None
    job_name: str | None
    activity_code: str | None
    activity_name: str | None
    activity_chargeable: bool | None
    # The service of the entry's job (an entry has none of its own).
    service_id: uuid.UUID | None = None
    service_name: str | None = None
    # The title of the entry's task (an entry has at most one).
    task_name: str | None = None


@dataclass(frozen=True, slots=True)
class EntryFilters:
    user_ids: Sequence[uuid.UUID] | None = None
    date_from: date | None = None
    date_to: date | None = None
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    billable: bool | None = None
    billing_statuses: Sequence[str] | None = None
    timesheet_statuses: Sequence[str] | None = None


def _view_select() -> Select[*tuple[Any, ...]]:
    return (
        select(
            TimeEntry,
            Timesheet.status,
            Client.code,
            Client.name,
            Job.number,
            Job.name,
            ActivityCode.code,
            ActivityCode.name,
            ActivityCode.is_chargeable,
            Job.service_id,
            Service.name,
            Task.title,
        )
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
        .outerjoin(
            Job, and_(Job.organization_id == TimeEntry.organization_id, Job.id == TimeEntry.job_id)
        )
        .outerjoin(
            ActivityCode,
            and_(
                ActivityCode.organization_id == TimeEntry.organization_id,
                ActivityCode.id == TimeEntry.activity_code_id,
            ),
        )
        .outerjoin(
            Service,
            and_(Service.organization_id == Job.organization_id, Service.id == Job.service_id),
        )
        .outerjoin(
            Task,
            and_(Task.organization_id == TimeEntry.organization_id, Task.id == TimeEntry.task_id),
        )
        .execution_options(populate_existing=True)
    )


def _to_view(row: Any) -> EntryView:
    return EntryView(row[0], *row[1:])


class TimeEntryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, entry: TimeEntry) -> TimeEntry:
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def delete(self, entry: TimeEntry) -> None:
        await self.session.delete(entry)
        await self.session.flush()

    async def get(
        self, organization_id: uuid.UUID, entry_id: uuid.UUID, *, for_update: bool = False
    ) -> TimeEntry | None:
        stmt = select(TimeEntry).where(
            TimeEntry.organization_id == organization_id, TimeEntry.id == entry_id
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def views(
        self, organization_id: uuid.UUID, entry_ids: Sequence[uuid.UUID]
    ) -> list[EntryView]:
        """Fresh views of the entries, in the order of ``entry_ids``."""
        if not entry_ids:
            return []
        stmt = _view_select().where(
            TimeEntry.organization_id == organization_id, TimeEntry.id.in_(list(entry_ids))
        )
        found = {
            view.entry.id: view for view in map(_to_view, (await self.session.execute(stmt)).all())
        }
        return [found[i] for i in entry_ids if i in found]

    async def list_page(
        self, organization_id: uuid.UUID, filters: EntryFilters, params: PageParams
    ) -> Page[EntryView]:
        """Newest first by ``(entry_date, id)``, keyset-paginated."""
        stmt = _view_select().where(TimeEntry.organization_id == organization_id)
        if filters.user_ids is not None:
            stmt = stmt.where(TimeEntry.user_id.in_(list(filters.user_ids)))
        if filters.date_from is not None:
            stmt = stmt.where(TimeEntry.entry_date >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(TimeEntry.entry_date <= filters.date_to)
        if filters.client_id is not None:
            stmt = stmt.where(TimeEntry.client_id == filters.client_id)
        if filters.job_id is not None:
            stmt = stmt.where(TimeEntry.job_id == filters.job_id)
        if filters.task_id is not None:
            stmt = stmt.where(TimeEntry.task_id == filters.task_id)
        if filters.billable is not None:
            stmt = stmt.where(TimeEntry.is_billable.is_(filters.billable))
        if filters.billing_statuses:
            stmt = stmt.where(TimeEntry.billing_status.in_(list(filters.billing_statuses)))
        if filters.timesheet_statuses:
            stmt = stmt.where(Timesheet.status.in_(list(filters.timesheet_statuses)))
        stmt = keyset_query(
            stmt,
            sort_expr=TimeEntry.entry_date,
            id_column=TimeEntry.id,
            params=params,
            descending=True,
            parse=parse_date,
        )
        rows = [_to_view(row) for row in (await self.session.execute(stmt)).all()]
        items, next_cursor = keyset_finish(rows, params, lambda v: (v.entry.entry_date, v.entry.id))
        return Page(items=items, next_cursor=next_cursor)

    async def range_views(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, date_from: date, date_to: date
    ) -> list[EntryView]:
        """All of a person's entries in a date range (a week: bounded), oldest first."""
        stmt = (
            _view_select()
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.user_id == user_id,
                TimeEntry.entry_date >= date_from,
                TimeEntry.entry_date <= date_to,
            )
            .order_by(TimeEntry.entry_date, TimeEntry.created_at, TimeEntry.id)
        )
        return [_to_view(row) for row in (await self.session.execute(stmt)).all()]

    async def sheet_views(
        self, organization_id: uuid.UUID, timesheet_id: uuid.UUID
    ) -> list[EntryView]:
        stmt = (
            _view_select()
            .where(
                TimeEntry.organization_id == organization_id, TimeEntry.timesheet_id == timesheet_id
            )
            .order_by(TimeEntry.entry_date, TimeEntry.created_at, TimeEntry.id)
        )
        return [_to_view(row) for row in (await self.session.execute(stmt)).all()]

    async def day_minutes(
        self,
        organization_id: uuid.UUID,
        user_id: uuid.UUID,
        days: Collection[date],
        *,
        exclude_ids: Collection[uuid.UUID] = (),
    ) -> dict[date, int]:
        """Minutes already logged by the person on each day (excluding some entries)."""
        if not days:
            return {}
        stmt = (
            select(TimeEntry.entry_date, func.sum(TimeEntry.minutes))
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.user_id == user_id,
                TimeEntry.entry_date.in_(list(days)),
                TimeEntry.id.not_in(list(exclude_ids)) if exclude_ids else true(),
            )
            .group_by(TimeEntry.entry_date)
        )
        return {row[0]: int(row[1]) for row in (await self.session.execute(stmt)).all()}


class TimerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, *, for_update: bool = False
    ) -> Timer | None:
        stmt = select(Timer).where(
            Timer.organization_id == organization_id, Timer.user_id == user_id
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def insert_if_absent(self, timer: Timer) -> bool:
        """Insert the person's timer. False when they already have one (race-safe)."""
        stmt = (
            insert(Timer)
            .values(
                id=timer.id,
                organization_id=timer.organization_id,
                user_id=timer.user_id,
                client_id=timer.client_id,
                job_id=timer.job_id,
                task_id=timer.task_id,
                activity_code_id=timer.activity_code_id,
                description=timer.description,
                is_billable=timer.is_billable,
                started_at=timer.started_at,
                accumulated_seconds=timer.accumulated_seconds,
            )
            .on_conflict_do_nothing(index_elements=[Timer.organization_id, Timer.user_id])
        )
        result: CursorResult[tuple[()]] = await self.session.execute(stmt)  # type: ignore[assignment]
        return result.rowcount > 0

    async def delete(self, timer: Timer) -> None:
        await self.session.delete(timer)
        await self.session.flush()

    async def delete_for_job(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> None:
        await self.session.execute(
            delete(Timer).where(Timer.organization_id == organization_id, Timer.job_id == job_id)
        )
