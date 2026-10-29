"""Timesheet queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    MemberPermissionOverride,
    Organization,
    OrganizationMember,
    Permission,
    RolePermission,
    TimeEntry,
    Timesheet,
)
from app.repositories.time_paging import keyset_finish, keyset_query, parse_date
from app.utils.pagination import Page, PageParams


@dataclass(frozen=True, slots=True)
class SheetTotals:
    entry_count: int = 0
    total_minutes: int = 0
    billable_minutes: int = 0
    billable_value: int = 0


EXEMPT_PERMISSION = "timesheet.exempt"


def sheet_counts_as_approved():
    """A sheet whose time counts as approved: approved, or its owner needs no approval."""
    return or_(Timesheet.status == "approved", Timesheet.approval_exempt.is_(True))


class TimesheetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def sync_exemptions(
        self, organization_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> None:
        """Mark the sheets of people who need no approval (one person's, or the practice's).

        A sheet is exempt when the practice's setting ``auto_approve_privileged_time`` is on and
        its owner is an active member whose role holds ``timesheet.exempt``. Called when a sheet
        is written and when the setting, a member's role or a role's permissions change.
        """
        role_gives = exists().where(
            OrganizationMember.organization_id == Timesheet.organization_id,
            OrganizationMember.user_id == Timesheet.user_id,
            OrganizationMember.status == "active",
            RolePermission.role_id == OrganizationMember.role_id,
            Permission.id == RolePermission.permission_id,
            Permission.code == EXEMPT_PERMISSION,
        )

        def own(effect: str):
            return exists().where(
                MemberPermissionOverride.organization_id == Timesheet.organization_id,
                MemberPermissionOverride.user_id == Timesheet.user_id,
                MemberPermissionOverride.permission_code == EXEMPT_PERMISSION,
                MemberPermissionOverride.effect == effect,
            )

        member_active = exists().where(
            OrganizationMember.organization_id == Timesheet.organization_id,
            OrganizationMember.user_id == Timesheet.user_id,
            OrganizationMember.status == "active",
        )
        # The role's, plus what was granted to the person, less what was denied them.
        holds = and_(or_(role_gives, and_(member_active, own("grant"))), ~own("deny"))
        enabled = (
            select(Organization.auto_approve_privileged_time)
            .where(Organization.id == organization_id)
            .scalar_subquery()
        )
        wanted = and_(enabled, holds)
        stmt = (
            update(Timesheet)
            .where(
                Timesheet.organization_id == organization_id,
                Timesheet.approval_exempt.is_distinct_from(wanted),
            )
            .values(approval_exempt=wanted)
            .execution_options(synchronize_session=False)
        )
        if user_id is not None:
            stmt = stmt.where(Timesheet.user_id == user_id)
        await self.session.execute(stmt)

    async def get(
        self, organization_id: uuid.UUID, timesheet_id: uuid.UUID, *, for_update: bool = False
    ) -> Timesheet | None:
        stmt = select(Timesheet).where(
            Timesheet.organization_id == organization_id, Timesheet.id == timesheet_id
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def find(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, week_start: date
    ) -> Timesheet | None:
        stmt = select(Timesheet).where(
            Timesheet.organization_id == organization_id,
            Timesheet.user_id == user_id,
            Timesheet.week_start == week_start,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def ensure(
        self, organization_id: uuid.UUID, user_id: uuid.UUID, week_start: date
    ) -> None:
        """Create the person's sheet for the week unless it exists (race-safe)."""
        await self.session.execute(
            insert(Timesheet)
            .values(organization_id=organization_id, user_id=user_id, week_start=week_start)
            .on_conflict_do_nothing(
                index_elements=[Timesheet.organization_id, Timesheet.user_id, Timesheet.week_start]
            )
        )

    async def lock_weeks(
        self, organization_id: uuid.UUID, weeks: Collection[tuple[uuid.UUID, date]]
    ) -> dict[tuple[uuid.UUID, date], Timesheet]:
        """Create (when missing) and lock the sheets of ``(user_id, week_start)`` pairs.

        Rows are locked in id order so concurrent writers never deadlock.
        """
        locked = await self.lock_for_write(organization_id, weeks)
        return {(sheet.user_id, sheet.week_start): sheet for sheet in locked.values()}

    async def lock_for_write(
        self,
        organization_id: uuid.UUID,
        weeks: Collection[tuple[uuid.UUID, date]],
        sheet_ids: Collection[uuid.UUID] = (),
    ) -> dict[uuid.UUID, Timesheet]:
        """Create (when missing) the sheets of ``(user_id, week_start)`` pairs, then lock
        them together with ``sheet_ids`` (existing sheets, e.g. the one an entry is on) in
        id order. Keyed by sheet id."""
        for user_id, week_start in sorted(weeks):
            await self.ensure(organization_id, user_id, week_start)
        for user_id in sorted({user_id for user_id, _ in weeks}):
            await self.sync_exemptions(organization_id, user_id)
        ids = set(sheet_ids)
        for user_id, week_start in weeks:
            sheet = await self.find(organization_id, user_id, week_start)
            assert sheet is not None
            ids.add(sheet.id)
        return {sheet.id: sheet for sheet in await self.lock_many(organization_id, ids)}

    async def lock_many(
        self, organization_id: uuid.UUID, timesheet_ids: Collection[uuid.UUID]
    ) -> list[Timesheet]:
        """Lock sheets ``FOR UPDATE`` in id order and return them fresh, in id order."""
        if not timesheet_ids:
            return []
        stmt = (
            select(Timesheet)
            .where(
                Timesheet.organization_id == organization_id,
                Timesheet.id.in_(list(set(timesheet_ids))),
            )
            .order_by(Timesheet.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def totals(
        self, organization_id: uuid.UUID, timesheet_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, SheetTotals]:
        if not timesheet_ids:
            return {}
        stmt = (
            select(
                TimeEntry.timesheet_id,
                func.count(),
                func.coalesce(func.sum(TimeEntry.minutes), 0),
                func.coalesce(
                    func.sum(case((TimeEntry.is_billable, TimeEntry.minutes), else_=0)), 0
                ),
                func.coalesce(
                    func.sum(case((TimeEntry.is_billable, TimeEntry.charge_value), else_=0)), 0
                ),
            )
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.timesheet_id.in_(list(timesheet_ids)),
            )
            .group_by(TimeEntry.timesheet_id)
        )
        return {
            row[0]: SheetTotals(int(row[1]), int(row[2]), int(row[3]), int(row[4]))
            for row in (await self.session.execute(stmt)).all()
        }

    async def lock_entry_statuses(
        self, organization_id: uuid.UUID, timesheet_id: uuid.UUID
    ) -> list[str]:
        """Lock the sheet's entries (id order) and return their billing statuses."""
        stmt = (
            select(TimeEntry.billing_status)
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.timesheet_id == timesheet_id,
            )
            .order_by(TimeEntry.id)
            .with_for_update()
        )
        return list((await self.session.execute(stmt)).scalars())

    async def lock_entries(
        self, organization_id: uuid.UUID, timesheet_id: uuid.UUID
    ) -> list[TimeEntry]:
        """The sheet's entries, locked (id order)."""
        stmt = (
            select(TimeEntry)
            .where(
                TimeEntry.organization_id == organization_id,
                TimeEntry.timesheet_id == timesheet_id,
            )
            .order_by(TimeEntry.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def delete(self, sheet: Timesheet) -> None:
        await self.session.delete(sheet)
        await self.session.flush()

    async def list_page(
        self,
        organization_id: uuid.UUID,
        *,
        user_ids: Sequence[uuid.UUID] | None,
        date_from: date | None,
        date_to: date | None,
        statuses: Sequence[str] | None,
        params: PageParams,
        descending: bool = True,
    ) -> Page[Timesheet]:
        """Sheets ordered by ``(week_start, id)``, keyset-paginated. ``user_ids=None``: all."""
        stmt = select(Timesheet).where(Timesheet.organization_id == organization_id)
        if user_ids is not None:
            stmt = stmt.where(Timesheet.user_id.in_(list(user_ids)))
        if date_from is not None:
            stmt = stmt.where(Timesheet.week_start >= date_from)
        if date_to is not None:
            stmt = stmt.where(Timesheet.week_start <= date_to)
        if statuses:
            stmt = stmt.where(Timesheet.status.in_(list(statuses)))
        stmt = keyset_query(
            stmt,
            sort_expr=Timesheet.week_start,
            id_column=Timesheet.id,
            params=params,
            descending=descending,
            parse=parse_date,
        )
        rows = list((await self.session.execute(stmt)).scalars())
        items, next_cursor = keyset_finish(rows, params, lambda s: (s.week_start, s.id))
        return Page(items=items, next_cursor=next_cursor)
