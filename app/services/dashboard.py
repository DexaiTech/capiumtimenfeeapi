"""The dashboard: the signed-in person's week, timer, timesheets and jobs, plus the
practice's headline figures for callers with ``report.financial``.

A fixed number of aggregate queries regardless of data size (no per-row queries). The
financial figures use the same definitions as the reports, so they tie out to the WIP,
aged-debtors and billing-summary reports run as at today.
"""

from datetime import date, timedelta

from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.dashboard import DashboardRepository
from app.schemas.dashboard import (
    ClientAmount,
    Dashboard,
    DashboardFinancial,
    DashboardTimer,
    DashboardWeek,
    DueJob,
    UnsubmittedWeek,
)
from app.services import reports
from app.services.policy import OrgContext, has_permission
from app.utils.money import percentage_bp

DUE_WITHIN_DAYS = 14
JOBS_LIMIT = 20
WEEKS_LIMIT = 12
TOP_LIMIT = 5


def week_start_of(day: date, week_start: int) -> date:
    """The practice's first weekday (ISO 1-7) on or before ``day``."""
    return day - timedelta(days=(day.isoweekday() - week_start) % 7)


def _client_amount(row: RowMapping) -> ClientAmount:
    return ClientAmount(
        client_id=row["client_id"],
        client_code=row["client_code"],
        client_name=row["client_name"],
        amount=int(row["amount"]),
        oldest_date=row["oldest_date"],
    )


class DashboardService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = DashboardRepository(session)

    async def get(self, ctx: OrgContext) -> Dashboard:
        org_id, user_id = ctx.organization_id, ctx.user.id
        today = reports.practice_today(ctx.organization)
        week_start = week_start_of(today, ctx.organization.week_start)
        week_end = week_start + timedelta(days=6)

        minutes = await self.repo.week_minutes(org_id, user_id, week_start, week_end)
        total_minutes = int(minutes["total_minutes"])
        chargeable = int(minutes["chargeable_minutes"])
        capacity = ctx.member.weekly_capacity_minutes
        week = DashboardWeek(
            week_start=week_start,
            week_end=week_end,
            total_minutes=total_minutes,
            chargeable_minutes=chargeable,
            non_chargeable_minutes=total_minutes - chargeable,
            capacity_minutes=capacity,
            utilisation_bp=percentage_bp(chargeable, capacity),
            target_bp=ctx.member.chargeable_target_bp,
        )

        weeks = await self.repo.unsubmitted_weeks(org_id, user_id, week_start, WEEKS_LIMIT)
        jobs = await self.repo.jobs_due(
            org_id, user_id, today + timedelta(days=DUE_WITHIN_DAYS), JOBS_LIMIT
        )

        approvals: int | None = None
        if has_permission(ctx, "timesheet.approve") or has_permission(ctx, "report.financial"):
            # Like the approvals queue: everyone's with time.read_all, else one's reports'.
            manager = None if has_permission(ctx, "time.read_all") else user_id
            approvals = await self.repo.approvals_waiting(org_id, manager)

        return Dashboard(
            today=today,
            week=week,
            timer=await self._timer(ctx),
            unsubmitted_week_count=int(weeks[0]["total_count"]) if weeks else 0,
            unsubmitted_weeks=[
                UnsubmittedWeek(
                    timesheet_id=w["timesheet_id"],
                    week_start=w["week_start"],
                    status=w["status"],
                    total_minutes=int(w["total_minutes"]),
                )
                for w in weeks
            ],
            jobs_due=[
                DueJob(
                    job_id=j["job_id"],
                    number=j["number"],
                    name=j["name"],
                    client_id=j["client_id"],
                    client_name=j["client_name"],
                    status=j["status"],
                    due_date=j["due_date"],
                    days_until_due=(j["due_date"] - today).days,
                    is_overdue=j["due_date"] < today,
                    is_manager=bool(j["is_manager"]),
                    is_assignee=bool(j["is_assignee"]),
                )
                for j in jobs
            ],
            approvals_waiting=approvals,
            financial=(
                await self._financial(ctx, today)
                if has_permission(ctx, "report.financial")
                else None
            ),
        )

    async def _timer(self, ctx: OrgContext) -> DashboardTimer | None:
        row = await self.repo.timer(ctx.organization_id, ctx.user.id)
        if row is None:
            return None
        started_at = row["started_at"]
        accumulated = int(row["accumulated_seconds"])
        running = 0
        if started_at is not None:
            running = max(0, int((reports.utc_now() - started_at).total_seconds()))
        return DashboardTimer(
            is_running=started_at is not None,
            started_at=started_at,
            accumulated_seconds=accumulated,
            elapsed_seconds=accumulated + running,
            client_id=row["client_id"],
            client_name=row["client_name"],
            job_id=row["job_id"],
            job_number=row["job_number"],
            job_name=row["job_name"],
            description=row["description"],
            is_billable=bool(row["is_billable"]),
        )

    async def _financial(self, ctx: OrgContext, today: date) -> DashboardFinancial:
        org_id = ctx.organization_id
        approval = ctx.organization.require_timesheet_approval
        wip = await self.repo.wip_totals(org_id, today, approval)
        debtors = await self.repo.debtor_totals(org_id, today)
        this_month = today.replace(day=1)
        next_month = (this_month + timedelta(days=32)).replace(day=1)
        last_month = (this_month - timedelta(days=1)).replace(day=1)
        billed_this, billed_last = await self.repo.billed_net(
            org_id,
            [
                (this_month, next_month - timedelta(days=1)),
                (last_month, this_month - timedelta(days=1)),
            ],
        )
        top_wip = await self.repo.top_wip_clients(org_id, today, approval, TOP_LIMIT)
        top_overdue = await self.repo.top_overdue_clients(org_id, today, TOP_LIMIT)
        return DashboardFinancial(
            wip_total=int(wip["total"]),
            wip_over_90_days=int(wip["over_90"]),
            debtors_total=int(debtors["total"]),
            overdue_total=int(debtors["overdue"]),
            billed_this_month=billed_this,
            billed_last_month=billed_last,
            top_clients_by_wip=[_client_amount(r) for r in top_wip],
            top_overdue_debtors=[_client_amount(r) for r in top_overdue],
        )
