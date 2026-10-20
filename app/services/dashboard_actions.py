"""My Actions: what the signed-in person has to do, most urgent first.

Each kind of action is worked out from the records of its own module, so the list always agrees
with the screens it links to: returned and unsubmitted timesheets, returned expense claims, the
approvals waiting, budget alerts and overdue jobs and tasks, and draft invoices.
"""

from collections import Counter
from datetime import date, datetime, timedelta
from typing import get_args

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.dashboard import DashboardRepository
from app.repositories.dashboard_pages import DashboardPagesRepository
from app.schemas.dashboard_pages import (
    ActionCounts,
    ActionKind,
    ActionPriority,
    ApprovalKind,
    MyAction,
    MyActions,
)
from app.services import reports
from app.services.dashboard import DUE_WITHIN_DAYS, JOBS_LIMIT, week_start_of
from app.services.dashboard_approvals import ApprovalQueue
from app.services.dashboard_common import logs_time
from app.services.policy import OrgContext, has_permission
from app.services.time_rules import practice_date
from app.services.timesheet_missing import MissingScope, last_completed_week, missing_timesheets

PRIORITY_RANK: dict[ActionPriority, int] = {"high": 0, "medium": 1, "low": 2}
KIND_RANK = {kind: rank for rank, kind in enumerate(get_args(ActionKind))}
SOON_DAYS = 7
LIST_LIMIT = 20

APPROVAL_ACTIONS: dict[ApprovalKind, tuple[ActionKind, str]] = {
    "timesheet": ("timesheet_approvals", "Timesheets waiting for your approval"),
    "expense": ("expense_approvals", "Expense claims waiting for your approval"),
    "invoice": ("invoice_approvals", "Invoices waiting for your approval"),
    "wip_adjustment": ("wip_adjustment_approvals", "WIP adjustments waiting for your approval"),
}


def _action(
    kind: ActionKind,
    priority: ActionPriority,
    title: str,
    link: str,
    *,
    ref: str = "",
    subject: str | None = None,
    context: str | None = None,
    day: date | None = None,
    days_until_due: int | None = None,
    count: int = 1,
    amount: int | None = None,
    usage_bp: int | None = None,
) -> MyAction:
    return MyAction(
        id=f"{kind}:{ref}" if ref else kind,
        kind=kind,
        priority=priority,
        title=title,
        subject=subject,
        context=context,
        date=day,
        days_until_due=days_until_due,
        count=count,
        amount=amount,
        usage_bp=usage_bp,
        link=link,
    )


def _sort_key(action: MyAction) -> tuple[int, int, date, str]:
    return (
        PRIORITY_RANK[action.priority],
        KIND_RANK[action.kind],
        action.date or date.max,
        action.id,
    )


class MyActionsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.base = DashboardRepository(session)
        self.repo = DashboardPagesRepository(session)
        self.queue = ApprovalQueue(session)

    async def get(self, ctx: OrgContext, limit: int) -> MyActions:
        today = reports.practice_today(ctx.organization)
        found = [
            *await self._timesheets(ctx, today),
            *await self._expenses(ctx),
            *await self._approvals(ctx),
            *await self._budgets(ctx),
            *await self._jobs(ctx, today),
            *await self._tasks(ctx, today),
            *await self._drafts(ctx),
        ]
        found.sort(key=_sort_key)
        tally = Counter(action.priority for action in found)
        return MyActions(
            visible=True,
            as_at=today,
            generated_at=reports.utc_now(),
            total=len(found),
            counts=ActionCounts(high=tally["high"], medium=tally["medium"], low=tally["low"]),
            actions=found[:limit],
        )

    async def _timesheets(self, ctx: OrgContext, today: date) -> list[MyAction]:
        if not logs_time(ctx):
            return []
        org = ctx.organization
        current_week = week_start_of(today, org.week_start)
        found: list[MyAction] = []
        backlog = await self.repo.timesheet_backlog(org.id, ctx.user.id, current_week)
        for row in backlog:
            if row["status"] == "rejected":
                found.append(
                    _action(
                        "timesheet_returned",
                        "high",
                        "Timesheets returned to you",
                        "/time",
                        day=row["oldest_week"],
                        count=int(row["count"]),
                    )
                )
            else:
                found.append(
                    _action(
                        "timesheet_to_submit",
                        "medium",
                        "Timesheets to submit",
                        "/time",
                        day=row["oldest_week"],
                        count=int(row["count"]),
                    )
                )
        week = last_completed_week(org, today)
        rows = await missing_timesheets(
            self.session,
            org.id,
            week,
            MissingScope(user_ids=frozenset({ctx.user.id})),
            organization=org,
            with_reminders=False,
        )
        if rows and rows[0].logged_minutes == 0:
            found.append(
                _action(
                    "timesheet_missing", "medium", "No time recorded last week", "/time", day=week
                )
            )
        return found

    async def _expenses(self, ctx: OrgContext) -> list[MyAction]:
        count, since = await self.repo.returned_claims(ctx.organization_id, ctx.user.id)
        if not count:
            return []
        return [
            _action(
                "expense_returned",
                "high",
                "Expense claims returned to you",
                "/expenses/mine",
                day=self._day(ctx, since),
                count=count,
            )
        ]

    async def _approvals(self, ctx: OrgContext) -> list[MyAction]:
        found: list[MyAction] = []
        for summary in await self.queue.summaries(ctx):
            if not summary.count:
                continue
            kind, title = APPROVAL_ACTIONS[summary.kind]
            found.append(
                _action(
                    kind,
                    "medium",
                    title,
                    summary.link,
                    day=summary.oldest_date,
                    count=summary.count,
                )
            )
        return found

    async def _budgets(self, ctx: OrgContext) -> list[MyAction]:
        rows = await self.repo.budget_alerts(ctx.organization_id, ctx.user.id, LIST_LIMIT)
        found: list[MyAction] = []
        for row in rows:
            over = int(row["threshold"]) >= 100
            found.append(
                _action(
                    "budget_exceeded" if over else "budget_warning",
                    "high" if over else "medium",
                    "Job over budget" if over else "Job close to its budget",
                    f"/jobs/{row['job_id']}",
                    ref=str(row["job_id"]),
                    subject=f"{row['job_number']} {row['job_name']}",
                    context=row["client_name"],
                    usage_bp=int(row["usage_bp"]),
                )
            )
        return found

    async def _jobs(self, ctx: OrgContext, today: date) -> list[MyAction]:
        rows = await self.base.jobs_due(
            ctx.organization_id, ctx.user.id, today + timedelta(days=DUE_WITHIN_DAYS), JOBS_LIMIT
        )
        found: list[MyAction] = []
        for row in rows:
            days = (row["due_date"] - today).days
            overdue = days < 0
            priority: ActionPriority = (
                "high" if overdue else "medium" if days <= SOON_DAYS else "low"
            )
            found.append(
                _action(
                    "job_overdue" if overdue else "job_due_soon",
                    priority,
                    "Job overdue" if overdue else "Job due soon",
                    f"/jobs/{row['job_id']}",
                    ref=str(row["job_id"]),
                    subject=f"{row['number']} {row['name']}",
                    context=row["client_name"],
                    day=row["due_date"],
                    days_until_due=days,
                )
            )
        return found

    async def _tasks(self, ctx: OrgContext, today: date) -> list[MyAction]:
        rows = await self.repo.overdue_tasks(ctx.organization_id, ctx.user.id, today, LIST_LIMIT)
        return [
            _action(
                "task_overdue",
                "high",
                "Task overdue",
                f"/jobs/{row['job_id']}",
                ref=str(row["task_id"]),
                subject=row["title"],
                context=f"{row['job_number']} {row['job_name']} ({row['client_name']})",
                day=row["due_date"],
                days_until_due=(row["due_date"] - today).days,
            )
            for row in rows
        ]

    async def _drafts(self, ctx: OrgContext) -> list[MyAction]:
        if not has_permission(ctx, "invoice.create"):
            return []
        count, gross, oldest = await self.repo.draft_invoices(ctx.organization_id, ctx.user.id)
        if not count:
            return []
        return [
            _action(
                "draft_invoices",
                "low",
                "Draft invoices to finish",
                "/billing/invoices",
                day=self._day(ctx, oldest),
                count=count,
                amount=gross,
            )
        ]

    @staticmethod
    def _day(ctx: OrgContext, moment: datetime | None) -> date | None:
        return practice_date(ctx.organization, moment) if moment else None
