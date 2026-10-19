"""The approvals waiting for the caller: timesheets and expense claims (and, as those modules
add them, invoices and WIP adjustments), in one queue.

The scope of each kind is the one of its own approval screen, so the dashboard and the screen
agree: timesheets of everyone with ``time.read_all`` or else of the people the caller manages,
expense claims of everyone with ``expense.read_all`` or else of the people the caller manages;
the caller's own are left out unless they hold ``organization.update`` (they may approve them).
"""

import uuid
from collections.abc import Collection

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.dashboard_pages import DashboardPagesRepository
from app.repositories.time_refs import TimeReferenceRepository
from app.repositories.timesheets import SheetTotals, TimesheetRepository
from app.schemas.dashboard_pages import ApprovalItem, ApprovalKind, ApprovalKindSummary
from app.services.dashboard_common import APPROVAL_LINKS, approval_kinds
from app.services.policy import OrgContext, has_permission
from app.services.time_rules import practice_date


class ApprovalQueue:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = DashboardPagesRepository(session)
        self.refs = TimeReferenceRepository(session)
        self._managed: set[uuid.UUID] | None = None

    async def _managed_ids(self, ctx: OrgContext) -> set[uuid.UUID]:
        if self._managed is None:
            self._managed = await self.refs.managed_user_ids(ctx.organization_id, ctx.user.id)
        return self._managed

    async def _scope(
        self, ctx: OrgContext, kind: ApprovalKind
    ) -> tuple[Collection[uuid.UUID] | None, uuid.UUID | None]:
        """Whose submissions the caller decides on (``None``: everyone's) and the person left
        out (the caller, who may not decide on their own)."""
        read_all = "time.read_all" if kind == "timesheet" else "expense.read_all"
        people = None if has_permission(ctx, read_all) else await self._managed_ids(ctx)
        own = None if has_permission(ctx, "organization.update") else ctx.user.id
        return people, own

    async def summaries(self, ctx: OrgContext) -> list[ApprovalKindSummary]:
        """One entry per kind the caller may decide, with how many are waiting."""
        found: list[ApprovalKindSummary] = []
        for kind in approval_kinds(ctx):
            people, own = await self._scope(ctx, kind)
            if people is not None and not people:
                count, oldest = 0, None
            elif kind == "timesheet":
                count, oldest = await self.repo.timesheet_queue(
                    ctx.organization_id, user_ids=people, exclude_user_id=own
                )
            else:
                count, moment = await self.repo.expense_queue(
                    ctx.organization_id, user_ids=people, exclude_user_id=own
                )
                oldest = practice_date(ctx.organization, moment) if moment else None
            found.append(
                ApprovalKindSummary(
                    kind=kind, count=count, oldest_date=oldest, link=APPROVAL_LINKS[kind]
                )
            )
        return found

    async def items(
        self, ctx: OrgContext, kinds: Collection[ApprovalKind], limit: int
    ) -> list[ApprovalItem]:
        """The oldest waiting approvals of these kinds (only those the caller may decide),
        oldest first."""
        allowed = [kind for kind in approval_kinds(ctx) if kind in kinds]
        items: list[ApprovalItem] = []
        for kind in allowed:
            people, own = await self._scope(ctx, kind)
            if people is not None and not people:
                continue
            if kind == "timesheet":
                items += await self._timesheets(ctx, people, own, limit)
            else:
                items += await self._claims(ctx, people, own, limit)
        items.sort(key=lambda item: (item.date, str(item.id)))
        return items[:limit]

    async def _timesheets(
        self,
        ctx: OrgContext,
        people: Collection[uuid.UUID] | None,
        own: uuid.UUID | None,
        limit: int,
    ) -> list[ApprovalItem]:
        rows = await self.repo.waiting_timesheets(
            ctx.organization_id, user_ids=people, exclude_user_id=own, limit=limit
        )
        totals = await TimesheetRepository(self.session).totals(
            ctx.organization_id, [row["id"] for row in rows]
        )
        return [
            ApprovalItem(
                kind="timesheet",
                id=row["id"],
                title=row["person"],
                subject=None,
                date=row["week_start"],
                submitted_at=row["submitted_at"],
                minutes=totals.get(row["id"], SheetTotals()).total_minutes,
                amount=None,
                link=APPROVAL_LINKS["timesheet"],
            )
            for row in rows
        ]

    async def _claims(
        self,
        ctx: OrgContext,
        people: Collection[uuid.UUID] | None,
        own: uuid.UUID | None,
        limit: int,
    ) -> list[ApprovalItem]:
        rows = await self.repo.waiting_expense_claims(
            ctx.organization_id, user_ids=people, exclude_user_id=own, limit=limit
        )
        return [
            ApprovalItem(
                kind="expense",
                id=row["id"],
                title=row["person"],
                subject=row["title"],
                date=practice_date(ctx.organization, row["submitted_at"]),
                submitted_at=row["submitted_at"],
                minutes=None,
                amount=int(row["gross"]),
                link=APPROVAL_LINKS["expense"],
            )
            for row in rows
        ]
