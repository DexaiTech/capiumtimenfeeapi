"""Rules shared by the dashboard pages: periods, comparisons and who sees which figures."""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from app.schemas.dashboard_pages import (
    ApprovalKind,
    CardComparison,
    ComparisonBasis,
    DashboardPeriod,
)
from app.services.policy import OrgContext, has_permission
from app.services.time_targets import WorkPattern
from app.utils.money import percentage_bp

PERIOD_LABELS: dict[DashboardPeriod, str] = {
    "this_week": "This Week",
    "this_month": "This Month",
    "ytd": "Year to Date",
}

# Who may decide each kind of approval, and where it is decided (a path inside the practice).
APPROVAL_PERMISSIONS: dict[ApprovalKind, str] = {
    "timesheet": "timesheet.approve",
    "expense": "expense.approve",
    "invoice": "invoice.approve",
    "wip_adjustment": "wip.approve",
}
APPROVAL_LINKS: dict[ApprovalKind, str] = {
    "timesheet": "/time/approvals",
    "expense": "/expenses/approvals",
    "invoice": "/billing/invoices",
    "wip_adjustment": "/wip/adjustments",
}
# The kinds the practice has today; each one also needs its permission.
LIVE_APPROVAL_KINDS: tuple[ApprovalKind, ...] = ("timesheet", "expense")


@dataclass(frozen=True, slots=True)
class PeriodWindow:
    """A period up to today and the same number of days a period earlier."""

    period: DashboardPeriod
    date_from: date
    date_to: date
    previous_from: date
    previous_to: date
    # The last day of the week or month (today for the year): how far the target reaches.
    horizon: date

    @property
    def before_start(self) -> date:
        """The day before the period started: the point a stock figure is compared with."""
        return self.date_from - timedelta(days=1)


def _month_length(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def resolve_period(period: DashboardPeriod, today: date, week_start: int) -> PeriodWindow:
    """The week (from the practice's first weekday), the month or the calendar year that ends
    today, with the equal stretch of the period before."""
    if period == "this_week":
        start = today - timedelta(days=(today.isoweekday() - week_start) % 7)
        previous_from = start - timedelta(days=7)
        return PeriodWindow(
            period,
            start,
            today,
            previous_from,
            previous_from + (today - start),
            start + timedelta(days=6),
        )
    if period == "this_month":
        start = today.replace(day=1)
        previous_from = (start - timedelta(days=1)).replace(day=1)
        previous_days = min(today.day, _month_length(previous_from.year, previous_from.month))
        horizon = today.replace(day=_month_length(today.year, today.month))
        return PeriodWindow(
            period,
            start,
            today,
            previous_from,
            previous_from + timedelta(days=previous_days - 1),
            horizon,
        )
    previous_year = today.year - 1
    return PeriodWindow(
        period,
        date(today.year, 1, 1),
        today,
        date(previous_year, 1, 1),
        date(previous_year, today.month, min(today.day, _month_length(previous_year, today.month))),
        today,
    )


def compare(basis: ComparisonBasis, value: int, previous: int) -> CardComparison:
    change = value - previous
    return CardComparison(
        basis=basis, previous=previous, change=change, change_bp=percentage_bp(change, previous)
    )


def target_between(pattern: WorkPattern, start: date, end: date) -> int:
    """The minutes a person is expected to record on the days of ``[start, end]``."""
    return sum(pattern.target_on(start + timedelta(days=i)) for i in range((end - start).days + 1))


# --- Who sees what --------------------------------------------------------------------------


def logs_time(ctx: OrgContext) -> bool:
    return has_permission(ctx, "time.log")


def sees_missing_timesheets(ctx: OrgContext) -> bool:
    return has_permission(ctx, "time.read_all") or has_permission(ctx, "timesheet.approve")


def sees_wip(ctx: OrgContext) -> bool:
    return has_permission(ctx, "wip.read") or has_permission(ctx, "report.financial")


def sees_billing(ctx: OrgContext) -> bool:
    return has_permission(ctx, "invoice.read") or has_permission(ctx, "report.financial")


def sees_debtors(ctx: OrgContext) -> bool:
    return (
        has_permission(ctx, "payment.read")
        or has_permission(ctx, "invoice.read")
        or has_permission(ctx, "report.financial")
    )


def approval_kinds(ctx: OrgContext) -> list[ApprovalKind]:
    """The kinds of approval the caller may decide."""
    return [kind for kind in LIVE_APPROVAL_KINDS if has_permission(ctx, APPROVAL_PERMISSIONS[kind])]
