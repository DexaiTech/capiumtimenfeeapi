"""The dashboard widgets with a page of their own: My Timesheet, Pending Approvals, Missing
Timesheets, Top Clients by WIP, Billing Trend and Debtor Ageing.

Every widget works out its own figures in a fixed number of queries and answers even when the
caller may not see it: ``visible: false`` and nothing else filled in.
"""

import datetime as dt
import uuid

from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.dashboard import DashboardRepository
from app.repositories.dashboard_pages import DashboardPagesRepository
from app.repositories.reports import DateRange, ItemFilter, ReportRepository
from app.repositories.time_overview import TimeOverviewRepository
from app.repositories.timesheets import TimesheetRepository
from app.schemas.dashboard_pages import (
    AgeingBucket,
    ApprovalKind,
    BillingMonth,
    BillingTrend,
    BillingWindow,
    BucketKey,
    DashboardDebtorRow,
    DashboardPeriod,
    DebtorAgeing,
    MissingTimesheetsWidget,
    MyTimesheet,
    MyTimesheetDay,
    MyTimesheetEntry,
    PendingApprovals,
    TopClientsByWip,
    WipClient,
)
from app.services import reports
from app.services.dashboard import DashboardService, week_start_of
from app.services.dashboard_approvals import ApprovalQueue
from app.services.dashboard_common import (
    approval_kinds,
    compare,
    logs_time,
    resolve_period,
    sees_billing,
    sees_debtors,
    sees_wip,
    target_between,
)
from app.services.dashboard_missing import missing_access
from app.services.policy import OrgContext, has_permission
from app.services.time_rules import week_start_for
from app.services.time_targets import pattern_of, week_figures
from app.services.timesheet_missing import last_completed_week, missing_timesheets
from app.utils.money import percentage_bp

ENTRY_LIMIT = 50
# The API's bucket names and the names of ``repositories.reports.debt_buckets``.
BUCKETS: tuple[tuple[BucketKey, str], ...] = (
    ("current", "current"),
    ("days_1_30", "days_1_30"),
    ("days_31_60", "days_31_60"),
    ("days_61_90", "days_61_90"),
    ("days_90_plus", "days_91_plus"),
)


def _month_starts(today: dt.date, months: int) -> list[dt.date]:
    """The first day of each of the last ``months`` months up to today's, oldest first."""
    year, month = today.year, today.month
    starts: list[dt.date] = []
    for _ in range(months):
        starts.append(dt.date(year, month, 1))
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return starts[::-1]


class DashboardWidgetsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.base = DashboardRepository(session)
        self.repo = DashboardPagesRepository(session)
        self.queue = ApprovalQueue(session)

    # --- My Timesheet ---------------------------------------------------------------------

    async def my_timesheet(self, ctx: OrgContext) -> MyTimesheet:
        org = ctx.organization
        today = reports.practice_today(org)
        start = week_start_of(today, org.week_start)
        stamp = {"as_at": today, "generated_at": reports.utc_now()}
        days = [start + dt.timedelta(days=i) for i in range(7)]
        if not logs_time(ctx):
            return MyTimesheet(
                visible=False,
                week_start=start,
                week_end=days[-1],
                timesheet_id=None,
                timesheet_status="open",
                decision_note=None,
                total_minutes=0,
                chargeable_minutes=0,
                non_chargeable_minutes=0,
                target_minutes=0,
                target_to_date_minutes=0,
                today_minutes=0,
                today_target_minutes=0,
                utilisation_bp=None,
                chargeable_target_bp=0,
                days=[],
                missing_days=[],
                entry_count=0,
                entries=[],
                timer=None,
                **stamp,
            )
        pattern = pattern_of(org, ctx.member)
        figures = await TimeOverviewRepository(self.session).day_figures(
            org.id, ctx.user.id, start, days[-1]
        )
        logged = {day: found.total_minutes for day, found in figures.items()}
        week = week_figures(pattern, days, logged, today)
        sheet = await TimesheetRepository(self.session).find(org.id, ctx.user.id, start)
        rows = await self.repo.week_entries(org.id, ctx.user.id, start, days[-1], ENTRY_LIMIT)
        total = sum(logged.values())
        chargeable = sum(found.billable_minutes for found in figures.values())
        return MyTimesheet(
            visible=True,
            week_start=start,
            week_end=days[-1],
            timesheet_id=sheet.id if sheet else None,
            timesheet_status="open" if sheet is None else sheet.status,
            decision_note=sheet.decision_note if sheet and sheet.status == "rejected" else None,
            total_minutes=total,
            chargeable_minutes=chargeable,
            non_chargeable_minutes=total - chargeable,
            target_minutes=week.weekly_target,
            target_to_date_minutes=target_between(pattern, start, today),
            today_minutes=logged.get(today, 0),
            today_target_minutes=week.daily_targets.get(today, 0),
            utilisation_bp=percentage_bp(chargeable, week.weekly_target),
            chargeable_target_bp=ctx.member.chargeable_target_bp,
            days=[
                MyTimesheetDay(
                    date=day,
                    minutes=logged.get(day, 0),
                    billable_minutes=figures[day].billable_minutes if day in figures else 0,
                    target_minutes=week.daily_targets[day],
                    is_working_day=pattern.is_working_day(day),
                    is_missing=day in week.missing_days,
                )
                for day in days
            ],
            missing_days=week.missing_days,
            entry_count=int(rows[0]["total_count"]) if rows else 0,
            entries=[
                MyTimesheetEntry(
                    id=row["id"],
                    entry_date=row["entry_date"],
                    minutes=int(row["minutes"]),
                    client_id=row["client_id"],
                    client_name=row["client_name"],
                    job_id=row["job_id"],
                    job_number=row["job_number"],
                    job_name=row["job_name"],
                    description=row["description"],
                    is_billable=bool(row["is_billable"]),
                    billing_status=row["billing_status"],
                )
                for row in rows
            ],
            timer=await DashboardService(self.session)._timer(ctx),
            **stamp,
        )

    # --- Pending approvals ----------------------------------------------------------------

    async def pending_approvals(
        self, ctx: OrgContext, kind: ApprovalKind | None, limit: int
    ) -> PendingApprovals:
        today = reports.practice_today(ctx.organization)
        summaries = await self.queue.summaries(ctx)
        kinds = [kind] if kind else approval_kinds(ctx)
        items = await self.queue.items(ctx, kinds, limit) if summaries else []
        return PendingApprovals(
            visible=bool(summaries),
            as_at=today,
            generated_at=reports.utc_now(),
            total=sum(s.count for s in summaries),
            kinds=summaries,
            items=items,
        )

    # --- Missing timesheets ---------------------------------------------------------------

    async def missing_timesheets(
        self, ctx: OrgContext, week_start: dt.date | None
    ) -> MissingTimesheetsWidget:
        org = ctx.organization
        today = reports.practice_today(org)
        last = last_completed_week(org, today)
        week = week_start_for(org, week_start) if week_start else last
        access = await missing_access(self.session, ctx)
        rows = (
            await missing_timesheets(self.session, org.id, week, access.narrow(), organization=org)
            if access
            else []
        )
        return MissingTimesheetsWidget(
            visible=access is not None,
            as_at=today,
            generated_at=reports.utc_now(),
            scope=access.scope if access else None,
            week_start=week,
            week_end=week + dt.timedelta(days=6),
            last_completed_week_start=last,
            count=len(rows),
            rows=rows,
            can_remind=access is not None
            and (
                has_permission(ctx, "organization.update")
                or has_permission(ctx, "timesheet.approve")
            ),
        )

    # --- Top clients by WIP ---------------------------------------------------------------

    async def top_clients_by_wip(
        self, ctx: OrgContext, limit: int, period: DashboardPeriod
    ) -> TopClientsByWip:
        org = ctx.organization
        today = reports.practice_today(org)
        window = resolve_period(period, today, org.week_start)
        stamp = {"as_at": today, "generated_at": reports.utc_now()}
        empty = TopClientsByWip(
            visible=False,
            period=period,
            date_from=window.date_from,
            total=0,
            previous_total=0,
            client_count=0,
            items=[],
            others_amount=0,
            **stamp,
        )
        if not sees_wip(ctx):
            return empty
        approval = org.require_timesheet_approval
        rows = await self.repo.wip_clients(org.id, today, approval, limit)
        previous = await self.repo.wip_amounts(
            org.id, window.before_start, approval, [row["client_id"] for row in rows]
        )
        before = await self.base.wip_totals(org.id, window.before_start, approval)
        everything = int(rows[0]["all_amount"]) if rows else 0
        items = [
            WipClient(
                client_id=row["client_id"],
                client_code=row["client_code"],
                client_name=row["client_name"],
                amount=int(row["amount"]),
                time_value=int(row["time_value"]),
                disbursement_value=int(row["disbursement_value"]),
                minutes=int(row["minutes"]),
                oldest_date=row["oldest_date"],
                over_90_days=int(row["over_90"]),
                share_bp=percentage_bp(int(row["amount"]), everything),
                previous_amount=previous.get(row["client_id"], 0),
                change=int(row["amount"]) - previous.get(row["client_id"], 0),
            )
            for row in rows
        ]
        return empty.model_copy(
            update={
                "visible": True,
                "total": everything,
                "previous_total": int(before["total"]),
                "client_count": int(rows[0]["client_count"]) if rows else 0,
                "items": items,
                "others_amount": everything - sum(item.amount for item in items),
            }
        )

    # --- Billing trend --------------------------------------------------------------------

    async def billing_trend(self, ctx: OrgContext, months: int) -> BillingTrend:
        org = ctx.organization
        today = reports.practice_today(org)
        starts = _month_starts(today, months)
        window = resolve_period("this_month", today, org.week_start)
        zero = BillingWindow(
            date_from=window.date_from,
            date_to=today,
            billed=0,
            credit_notes=0,
            net_billed=0,
            receipts=0,
        )
        stamp = {"as_at": today, "generated_at": reports.utc_now()}
        if not sees_billing(ctx):
            return BillingTrend(
                visible=False,
                months=months,
                series=[],
                this_month=zero,
                last_month=zero.model_copy(
                    update={"date_from": window.previous_from, "date_to": window.previous_to}
                ),
                billed_comparison=compare("previous_period", 0, 0),
                **stamp,
            )
        reader = ReportRepository(self.session)
        invoices, credits, receipts = await reader.billing_documents(
            org.id, DateRange(starts[0], today), "month", ItemFilter()
        )
        billed = {row["key"]: row for row in invoices}
        credited = {row["key"]: row for row in credits}
        received = {row["key"]: int(row["amount"]) for row in receipts}
        series = [
            BillingMonth(
                month=month,
                billed=int(billed[month]["net"]) if month in billed else 0,
                invoice_count=int(billed[month]["count"]) if month in billed else 0,
                credit_notes=int(credited[month]["net"]) if month in credited else 0,
                credit_note_count=int(credited[month]["count"]) if month in credited else 0,
                net_billed=(int(billed[month]["net"]) if month in billed else 0)
                - (int(credited[month]["net"]) if month in credited else 0),
                receipts=received.get(month, 0),
            )
            for month in starts
        ]
        current = series[-1]
        this_month = BillingWindow(
            date_from=window.date_from,
            date_to=today,
            billed=current.billed,
            credit_notes=current.credit_notes,
            net_billed=current.net_billed,
            receipts=current.receipts,
        )
        last_month = await self._billing_window(org.id, window.previous_from, window.previous_to)
        return BillingTrend(
            visible=True,
            months=months,
            series=series,
            this_month=this_month,
            last_month=last_month,
            billed_comparison=compare("previous_period", this_month.billed, last_month.billed),
            **stamp,
        )

    async def _billing_window(
        self, organization_id: uuid.UUID, start: dt.date, end: dt.date
    ) -> BillingWindow:
        invoices, credits, receipts = await ReportRepository(self.session).billing_documents(
            organization_id, DateRange(start, end), "month", ItemFilter()
        )
        billed = sum(int(row["net"]) for row in invoices)
        credited = sum(int(row["net"]) for row in credits)
        return BillingWindow(
            date_from=start,
            date_to=end,
            billed=billed,
            credit_notes=credited,
            net_billed=billed - credited,
            receipts=sum(int(row["amount"]) for row in receipts),
        )

    # --- Debtor ageing --------------------------------------------------------------------

    async def debtor_ageing(
        self, ctx: OrgContext, limit: int, period: DashboardPeriod
    ) -> DebtorAgeing:
        org = ctx.organization
        today = reports.practice_today(org)
        window = resolve_period(period, today, org.week_start)
        stamp = {"as_at": today, "generated_at": reports.utc_now()}
        empty = DebtorAgeing(
            visible=False,
            period=period,
            date_from=window.date_from,
            total=0,
            overdue_total=0,
            previous_total=0,
            previous_overdue_total=0,
            invoice_count=0,
            debtor_count=0,
            buckets=[],
            top_debtors=[],
            **stamp,
        )
        if not sees_debtors(ctx):
            return empty
        breakdown = await self.repo.debtor_breakdown(org.id, today)
        rows = await self.repo.top_debtors(org.id, today, limit)
        before = await self.base.debtor_totals(org.id, window.before_start)
        total = int(breakdown["total"])
        buckets = [
            AgeingBucket(
                key=key,
                amount=int(breakdown[source]),
                invoice_count=int(breakdown[f"n_{source}"]),
                share_bp=percentage_bp(int(breakdown[source]), total),
            )
            for key, source in BUCKETS
        ]
        return empty.model_copy(
            update={
                "visible": True,
                "total": total,
                "overdue_total": total - int(breakdown["current"]),
                "previous_total": int(before["total"]),
                "previous_overdue_total": int(before["overdue"]),
                "invoice_count": int(breakdown["invoice_count"]),
                "debtor_count": int(breakdown["debtor_count"]),
                "buckets": buckets,
                "top_debtors": [_debtor(row) for row in rows],
            }
        )


def _debtor(row: RowMapping) -> DashboardDebtorRow:
    return DashboardDebtorRow(
        client_id=row["client_id"],
        client_code=row["client_code"],
        client_name=row["client_name"],
        invoice_count=int(row["invoice_count"]),
        total=int(row["total"]),
        overdue=int(row["overdue"]),
        oldest_due_date=row["oldest_due_date"],
        current=int(row["current"]),
        days_1_30=int(row["days_1_30"]),
        days_31_60=int(row["days_31_60"]),
        days_61_90=int(row["days_61_90"]),
        days_90_plus=int(row["days_91_plus"]),
    )
