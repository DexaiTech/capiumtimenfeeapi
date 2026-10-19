"""The six summary cards of the dashboard for a period (This Week, This Month, Year to Date).

A card is a figure, how it compares and where to read more. Figures that flow (hours, billing)
are compared with the same number of days a period earlier; figures that stand (WIP, debtors)
with their value the day before the period started. A card the caller has no permission for
stays in the list with ``visible: false`` and no figure.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.dashboard import DashboardRepository
from app.schemas.dashboard_pages import (
    CardFigure,
    CardKey,
    CardUnit,
    DashboardCard,
    DashboardCards,
    DashboardPeriod,
)
from app.services import reports
from app.services.dashboard_approvals import ApprovalQueue
from app.services.dashboard_common import (
    PERIOD_LABELS,
    PeriodWindow,
    compare,
    logs_time,
    resolve_period,
    sees_billing,
    sees_debtors,
    sees_wip,
    target_between,
)
from app.services.dashboard_missing import (
    missing_access,
    missing_person_weeks,
    weeks_of_window,
)
from app.services.policy import OrgContext
from app.services.time_targets import pattern_of

LINKS: dict[CardKey, str] = {
    "my_hours": "/dashboard/my-timesheet",
    "missing_timesheets": "/dashboard/missing-timesheets",
    "pending_approvals": "/dashboard/pending-approvals",
    "unbilled_wip": "/dashboard/top-clients-by-wip",
    "billed": "/dashboard/billing-trend",
    "overdue_debtors": "/dashboard/debtor-ageing",
}


def _hidden(key: CardKey, title: str, unit: CardUnit) -> DashboardCard:
    return DashboardCard(
        key=key,
        title=title,
        visible=False,
        unit=unit,
        value=None,
        comparison=None,
        secondary=None,
        link=LINKS[key],
    )


class DashboardCardsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.base = DashboardRepository(session)
        self.queue = ApprovalQueue(session)

    async def get(self, ctx: OrgContext, period: DashboardPeriod) -> DashboardCards:
        org = ctx.organization
        today = reports.practice_today(org)
        window = resolve_period(period, today, org.week_start)
        label = PERIOD_LABELS[period]
        cards = [
            await self._my_hours(ctx, window, label),
            await self._missing(ctx, window),
            await self._approvals(ctx),
            await self._wip(ctx, window),
            await self._billed(ctx, window, label),
            await self._debtors(ctx, window),
        ]
        return DashboardCards(
            period=period,
            date_from=window.date_from,
            date_to=window.date_to,
            previous_from=window.previous_from,
            previous_to=window.previous_to,
            as_at=today,
            generated_at=reports.utc_now(),
            cards=cards,
        )

    async def _my_hours(self, ctx: OrgContext, window: PeriodWindow, label: str) -> DashboardCard:
        title = f"My Hours {label}"
        if not logs_time(ctx):
            return _hidden("my_hours", title, "minutes")
        org_id, user_id = ctx.organization_id, ctx.user.id
        now = await self.base.week_minutes(org_id, user_id, window.date_from, window.date_to)
        before = await self.base.week_minutes(
            org_id, user_id, window.previous_from, window.previous_to
        )
        pattern = pattern_of(ctx.organization, ctx.member)
        target = target_between(pattern, window.date_from, window.horizon)
        value = int(now["total_minutes"])
        return DashboardCard(
            key="my_hours",
            title=title,
            visible=True,
            unit="minutes",
            value=value,
            comparison=compare("previous_period", value, int(before["total_minutes"])),
            secondary=CardFigure(key="target", value=target, unit="minutes"),
            link=LINKS["my_hours"],
        )

    async def _missing(self, ctx: OrgContext, window: PeriodWindow) -> DashboardCard:
        access = await missing_access(self.session, ctx)
        if access is None:
            return _hidden("missing_timesheets", "Missing Timesheets", "count")
        org = ctx.organization
        weeks = weeks_of_window(org, window.date_from, window.date_to)
        found = await missing_person_weeks(self.session, org, weeks, access.user_ids)
        return DashboardCard(
            key="missing_timesheets",
            title="Missing Timesheets",
            visible=True,
            unit="count",
            value=len(found),
            comparison=None,
            secondary=CardFigure(
                key="people", value=len({person for _, person in found}), unit="count"
            ),
            link=LINKS["missing_timesheets"],
        )

    async def _approvals(self, ctx: OrgContext) -> DashboardCard:
        summaries = await self.queue.summaries(ctx)
        if not summaries:
            return _hidden("pending_approvals", "Pending Approvals", "count")
        return DashboardCard(
            key="pending_approvals",
            title="Pending Approvals",
            visible=True,
            unit="count",
            value=sum(s.count for s in summaries),
            comparison=None,
            secondary=None,
            link=LINKS["pending_approvals"],
        )

    async def _wip(self, ctx: OrgContext, window: PeriodWindow) -> DashboardCard:
        if not sees_wip(ctx):
            return _hidden("unbilled_wip", "Unbilled WIP", "money")
        org = ctx.organization
        approval = org.require_timesheet_approval
        now = await self.base.wip_totals(org.id, window.date_to, approval)
        before = await self.base.wip_totals(org.id, window.before_start, approval)
        value = int(now["total"])
        return DashboardCard(
            key="unbilled_wip",
            title="Unbilled WIP",
            visible=True,
            unit="money",
            value=value,
            comparison=compare("period_start", value, int(before["total"])),
            secondary=CardFigure(key="over_90_days", value=int(now["over_90"]), unit="money"),
            link=LINKS["unbilled_wip"],
        )

    async def _billed(self, ctx: OrgContext, window: PeriodWindow, label: str) -> DashboardCard:
        title = f"Billed {label}"
        if not sees_billing(ctx):
            return _hidden("billed", title, "money")
        value, before = await self.base.billed_net(
            ctx.organization_id,
            [
                (window.date_from, window.date_to),
                (window.previous_from, window.previous_to),
            ],
        )
        return DashboardCard(
            key="billed",
            title=title,
            visible=True,
            unit="money",
            value=value,
            comparison=compare("previous_period", value, before),
            secondary=None,
            link=LINKS["billed"],
        )

    async def _debtors(self, ctx: OrgContext, window: PeriodWindow) -> DashboardCard:
        if not sees_debtors(ctx):
            return _hidden("overdue_debtors", "Overdue Debtors", "money")
        org_id = ctx.organization_id
        now = await self.base.debtor_totals(org_id, window.date_to)
        before = await self.base.debtor_totals(org_id, window.before_start)
        value = int(now["overdue"])
        return DashboardCard(
            key="overdue_debtors",
            title="Overdue Debtors",
            visible=True,
            unit="money",
            value=value,
            comparison=compare("period_start", value, int(before["overdue"])),
            secondary=CardFigure(key="total_outstanding", value=int(now["total"]), unit="money"),
            link=LINKS["overdue_debtors"],
        )
