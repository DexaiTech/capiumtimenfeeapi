"""The dashboard pages: summary cards, My Actions and the widgets behind them. HTTP only.

Every widget has its own route so a page can load, refresh and fail one widget at a time. A
widget the caller has no permission for answers 200 with ``visible: false``; the rules are in the
``dashboard_*`` services (docs/modules/dashboard.md).
"""

import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.dependencies.common import SessionDep
from app.dependencies.organization import OrgContextDep
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.dashboard_pages import (
    ApprovalKind,
    BillingTrend,
    DashboardCards,
    DashboardPeriod,
    DebtorAgeing,
    MissingTimesheetsWidget,
    MyActions,
    MyTimesheet,
    PendingApprovals,
    TopClientsByWip,
)
from app.schemas.time import MAX_TIME_DATE, MIN_TIME_DATE
from app.services.dashboard_actions import MyActionsService
from app.services.dashboard_cards import DashboardCardsService
from app.services.dashboard_widgets import DashboardWidgetsService

router = APIRouter(prefix="/organizations/{organization_id}/dashboard", tags=["Dashboard"])


def get_cards_service(session: SessionDep) -> DashboardCardsService:
    return DashboardCardsService(session)


def get_actions_service(session: SessionDep) -> MyActionsService:
    return MyActionsService(session)


def get_widgets_service(session: SessionDep) -> DashboardWidgetsService:
    return DashboardWidgetsService(session)


CardsServiceDep = Annotated[DashboardCardsService, Depends(get_cards_service)]
ActionsServiceDep = Annotated[MyActionsService, Depends(get_actions_service)]
WidgetsServiceDep = Annotated[DashboardWidgetsService, Depends(get_widgets_service)]

PeriodQuery = Annotated[
    DashboardPeriod,
    Query(
        description=(
            "`this_week` (from the practice's first weekday), `this_month` or `ytd` (calendar "
            "year to date): the period that ends today"
        )
    ),
]
_VISIBLE = (
    " Without the permissions behind it the widget still answers 200, with `visible: false` "
    "and nothing else filled in."
)


@router.get(
    "/cards",
    summary="Dashboard cards",
    description=(
        "The six summary cards for a period: My Hours (`time.log`), Missing Timesheets "
        "(`time.read_all` or `timesheet.approve`), Pending Approvals (an approve permission), "
        "Unbilled WIP (`wip.read` or `report.financial`), Billed (`invoice.read` or "
        "`report.financial`) and Overdue Debtors (`payment.read`, `invoice.read` or "
        "`report.financial`). Each has its figure, how it compares (hours and billing with the "
        "same days a period earlier, WIP and debtors with the day before the period began), a "
        "second figure and the page to read more. A card the caller may not see is still "
        "listed, with `visible: false` and no figure. Dates are in the practice time zone."
    ),
    responses=error_responses(404),
)
async def get_cards(
    org: OrgContextDep, service: CardsServiceDep, period: PeriodQuery = "this_week"
) -> ApiResponse[DashboardCards]:
    return ok(await service.get(org, period))


@router.get(
    "/my-actions",
    summary="My actions",
    description=(
        "What the caller has to do, most urgent first (high, then medium, then low): "
        "timesheets returned or still to submit, last week's missing time, expense claims "
        "returned, approvals waiting for them (timesheets and expense claims), jobs over or "
        "near their budget, jobs overdue or due within 14 days that they manage or are "
        "assigned to, tasks assigned to them that are overdue, and draft invoices they "
        "prepared. Every action carries the page where it is dealt with. `total` and `counts` "
        "cover all of them; `actions` is cut at `limit`."
    ),
    responses=error_responses(404),
)
async def get_my_actions(
    org: OrgContextDep,
    service: ActionsServiceDep,
    limit: Annotated[int, Query(ge=1, le=100, description="Most actions to list")] = 50,
) -> ApiResponse[MyActions]:
    return ok(await service.get(org, limit))


@router.get(
    "/my-timesheet",
    summary="My timesheet this week",
    description=(
        "The caller's current week (from the practice's first weekday): hours against the "
        "target of the week and of the days so far, today, each day with its target and "
        "whether it is a working day with no time, the status of the timesheet (and the note "
        "of a returned one), the latest 50 entries and the running timer. Needs `time.log`."
        + _VISIBLE
    ),
    responses=error_responses(404),
)
async def get_my_timesheet(
    org: OrgContextDep, service: WidgetsServiceDep
) -> ApiResponse[MyTimesheet]:
    return ok(await service.my_timesheet(org))


@router.get(
    "/pending-approvals",
    summary="Pending approvals",
    description=(
        "Everything waiting for the caller to approve, in one queue: submitted timesheets "
        "(`timesheet.approve`; everyone's with `time.read_all`, otherwise the people the caller "
        "manages) and submitted expense claims (`expense.approve`; everyone's with "
        "`expense.read_all`, otherwise the people the caller manages). The caller's own are "
        "left out unless they hold `organization.update`. `kinds` counts each kind the caller "
        "may decide; `items` lists the oldest waiting ones, optionally of one `kind`. "
        "Approving happens on the module's own endpoints." + _VISIBLE
    ),
    responses=error_responses(404),
)
async def get_pending_approvals(
    org: OrgContextDep,
    service: WidgetsServiceDep,
    kind: Annotated[
        ApprovalKind | None,
        Query(description="List only this kind (`timesheet` or `expense`)"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=50, description="Most items to list")] = 10,
) -> ApiResponse[PendingApprovals]:
    return ok(await service.pending_approvals(org, kind, limit))


@router.get(
    "/missing-timesheets",
    summary="Missing timesheets",
    description=(
        "Staff whose timesheet for a week is not submitted, as `GET /timesheets/missing` lists "
        "them, for the people the caller may see: everyone with `time.read_all`, otherwise "
        "(with `timesheet.approve`) the people the caller manages and the caller. `week_start` "
        "is any day of the week (default: the last completed week). `can_remind` says whether "
        "the caller may send the reminders (`POST /timesheets/reminders/run`)." + _VISIBLE
    ),
    responses=error_responses(404),
)
async def get_missing_timesheets(
    org: OrgContextDep,
    service: WidgetsServiceDep,
    week_start: Annotated[
        dt.date | None,
        Query(
            description="Any day of the week (default: the last completed week)",
            ge=MIN_TIME_DATE,
            le=MAX_TIME_DATE,
        ),
    ] = None,
) -> ApiResponse[MissingTimesheetsWidget]:
    return ok(await service.missing_timesheets(org, week_start))


@router.get(
    "/top-clients-by-wip",
    summary="Top clients by WIP",
    description=(
        "The clients with the most unbilled work in progress as at today (time and "
        "disbursements, counted as the WIP report counts them), largest first, with each "
        "client's share and how its WIP moved since the day before the period began, and what "
        "the clients not listed hold. Needs `wip.read` or `report.financial`." + _VISIBLE
    ),
    responses=error_responses(404),
)
async def get_top_clients_by_wip(
    org: OrgContextDep,
    service: WidgetsServiceDep,
    limit: Annotated[int, Query(ge=1, le=50, description="Most clients to list")] = 10,
    period: PeriodQuery = "this_month",
) -> ApiResponse[TopClientsByWip]:
    return ok(await service.top_clients_by_wip(org, limit, period))


@router.get(
    "/billing-trend",
    summary="Billing trend",
    description=(
        "Per month for the last `months` months (this one included, empty months too): the net "
        "of invoices issued, the net of credit notes, net billed and the receipts (payments "
        "received, not voided). With this month so far against the same days last month. Needs "
        "`invoice.read` or `report.financial`." + _VISIBLE
    ),
    responses=error_responses(404),
)
async def get_billing_trend(
    org: OrgContextDep,
    service: WidgetsServiceDep,
    months: Annotated[int, Query(ge=1, le=24, description="Months to show, this one last")] = 12,
) -> ApiResponse[BillingTrend]:
    return ok(await service.billing_trend(org, months))


@router.get(
    "/debtor-ageing",
    summary="Debtor ageing",
    description=(
        "The outstanding balance of issued invoices as at today in five age buckets by days "
        "past the due date (current, 1-30, 31-60, 61-90, 90+), with the clients that owe the "
        "most overdue, and the totals as at the day before the period began. Counted as the "
        "aged debtors report counts them. Needs `payment.read`, `invoice.read` or "
        "`report.financial`." + _VISIBLE
    ),
    responses=error_responses(404),
)
async def get_debtor_ageing(
    org: OrgContextDep,
    service: WidgetsServiceDep,
    limit: Annotated[int, Query(ge=1, le=50, description="Most debtors to list")] = 10,
    period: PeriodQuery = "this_month",
) -> ApiResponse[DebtorAgeing]:
    return ok(await service.debtor_ageing(org, limit, period))
