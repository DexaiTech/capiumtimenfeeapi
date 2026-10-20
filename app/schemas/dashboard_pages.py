"""Response models of the dashboard pages: cards, My Actions, My Timesheet, Pending Approvals,
Missing Timesheets, Top Clients by WIP, Billing Trend and Debtor Ageing."""

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.dashboard import DashboardTimer
from app.schemas.time import TimesheetStatus
from app.schemas.time_views import MissingTimesheet

DashboardPeriod = Literal["this_week", "this_month", "ytd"]
CardKey = Literal[
    "my_hours",
    "missing_timesheets",
    "pending_approvals",
    "unbilled_wip",
    "billed",
    "overdue_debtors",
]
CardUnit = Literal["minutes", "count", "money"]
ComparisonBasis = Literal["previous_period", "period_start"]
ApprovalKind = Literal["timesheet", "expense", "invoice", "wip_adjustment"]
ActionKind = Literal[
    "timesheet_returned",
    "expense_returned",
    "task_overdue",
    "job_overdue",
    "budget_exceeded",
    "timesheet_to_submit",
    "timesheet_missing",
    "timesheet_approvals",
    "expense_approvals",
    "invoice_approvals",
    "wip_adjustment_approvals",
    "budget_warning",
    "job_due_soon",
    "draft_invoices",
]
ActionPriority = Literal["high", "medium", "low"]
BucketKey = Literal["current", "days_1_30", "days_31_60", "days_61_90", "days_90_plus"]
MissingScopeName = Literal["practice", "team"]


class WidgetBase(BaseModel):
    """What every widget answers: whether the caller may see it, and when it was worked out."""

    visible: bool = Field(
        description="False when the caller's permissions do not include this widget: every other "
        "field is then empty (null, 0 or an empty list). The request itself still answers 200."
    )
    as_at: dt.date = Field(description="Today in the practice time zone")
    generated_at: dt.datetime = Field(description="When the figures were worked out (UTC)")


# --- Cards ---------------------------------------------------------------------------------


class CardComparison(BaseModel):
    basis: ComparisonBasis = Field(
        description="`previous_period`: the same elapsed days of the previous week, month or "
        "year (hours, billing). `period_start`: the figure as at the day before the period "
        "started (WIP, debtors), so the change is the movement within the period"
    )
    previous: int
    change: int = Field(description="Value minus previous")
    change_bp: int | None = Field(
        description="`change` as basis points of `previous` (1000 = +10%); null when "
        "`previous` is 0"
    )


class CardFigure(BaseModel):
    key: str = Field(
        description="`target` (minutes), `people` (distinct staff), `over_90_days` (money) or "
        "`total_outstanding` (money)"
    )
    value: int
    unit: CardUnit


class DashboardCard(BaseModel):
    key: CardKey
    title: str = Field(description="Names the period where the figure follows it")
    visible: bool = Field(description="False without the permissions behind the figure")
    unit: CardUnit = Field(description="`minutes`, `count` or `money` (pence)")
    value: int | None
    comparison: CardComparison | None = Field(
        description="Null for the queue cards (missing timesheets, approvals) and when hidden"
    )
    secondary: CardFigure | None
    link: str = Field(description="A path inside the practice (below `/{slug}`)")


class DashboardCards(BaseModel):
    period: DashboardPeriod
    date_from: dt.date = Field(description="First day of the period")
    date_to: dt.date = Field(description="Last day counted: today")
    previous_from: dt.date
    previous_to: dt.date = Field(
        description="The same number of days as the period, a period earlier"
    )
    as_at: dt.date
    generated_at: dt.datetime
    cards: list[DashboardCard] = Field(
        description="The six cards in a fixed order; hidden ones stay with `visible: false`"
    )


# --- My Actions ----------------------------------------------------------------------------


class MyAction(BaseModel):
    id: str = Field(description="Stable: the kind and what it is about")
    kind: ActionKind
    priority: ActionPriority
    title: str
    subject: str | None = Field(description="What it is about: a job, a task, a client")
    context: str | None = Field(description="Where it belongs, for example the client of a job")
    date: dt.date | None = Field(
        description="The due date, or the oldest week or submission the action covers"
    )
    days_until_due: int | None = Field(description="Negative when overdue; null without a date")
    count: int = Field(description="How many records the action stands for")
    amount: int | None = Field(description="Pence, for draft invoices")
    usage_bp: int | None = Field(description="Budget used in basis points, for budget alerts")
    link: str = Field(description="A path inside the practice (below `/{slug}`)")


class ActionCounts(BaseModel):
    high: int
    medium: int
    low: int


class MyActions(WidgetBase):
    """Every member has actions: `visible` is always true (it keeps the widget shape)."""

    total: int = Field(description="All the caller's actions, before `limit`")
    counts: ActionCounts
    actions: list[MyAction] = Field(description="Most urgent first, at most `limit`")


# --- My Timesheet --------------------------------------------------------------------------


class MyTimesheetDay(BaseModel):
    date: dt.date
    minutes: int
    billable_minutes: int
    target_minutes: int = Field(description="0 on a day off")
    is_working_day: bool
    is_missing: bool = Field(description="A working day up to today with no time logged")


class MyTimesheetEntry(BaseModel):
    id: uuid.UUID
    entry_date: dt.date
    minutes: int
    client_id: uuid.UUID | None
    client_name: str | None
    job_id: uuid.UUID | None
    job_number: str | None
    job_name: str | None
    description: str | None
    is_billable: bool
    billing_status: str


class MyTimesheet(WidgetBase):
    week_start: dt.date
    week_end: dt.date
    timesheet_id: uuid.UUID | None
    timesheet_status: TimesheetStatus = Field(description="'open' when the week has no sheet yet")
    decision_note: str | None = Field(description="Why the sheet was returned")
    total_minutes: int
    chargeable_minutes: int
    non_chargeable_minutes: int
    target_minutes: int = Field(description="The target of the whole week")
    target_to_date_minutes: int = Field(description="The target of the days up to today")
    today_minutes: int
    today_target_minutes: int
    utilisation_bp: int | None = Field(
        description="Chargeable minutes over the week's target, basis points"
    )
    chargeable_target_bp: int = Field(description="The person's chargeable target")
    days: list[MyTimesheetDay] = Field(description="The seven days of the week")
    missing_days: list[dt.date]
    entry_count: int = Field(description="Entries of the week (`entries` shows the latest 50)")
    entries: list[MyTimesheetEntry] = Field(description="Newest first")
    timer: DashboardTimer | None


# --- Pending approvals ---------------------------------------------------------------------


class ApprovalKindSummary(BaseModel):
    kind: ApprovalKind
    count: int
    oldest_date: dt.date | None = Field(
        description="The oldest waiting week (timesheets) or submission day"
    )
    link: str = Field(description="The module page where they are decided")


class ApprovalItem(BaseModel):
    kind: ApprovalKind
    id: uuid.UUID
    title: str = Field(description="Whose timesheet or claim, or the invoice number")
    subject: str | None = Field(description="The claim's title, or the invoice's client")
    date: dt.date = Field(description="The week of a timesheet; the submission day otherwise")
    submitted_at: dt.datetime | None
    minutes: int | None = Field(description="Total minutes of a timesheet")
    amount: int | None = Field(description="Pence: an expense claim's gross, an invoice's gross")
    link: str = Field(description="A path inside the practice (below `/{slug}`)")


class PendingApprovals(WidgetBase):
    total: int = Field(description="Everything waiting for the caller, all kinds")
    kinds: list[ApprovalKindSummary] = Field(
        description="One entry per kind the caller may decide, with its count"
    )
    items: list[ApprovalItem] = Field(description="Oldest first, at most `limit`")


# --- Missing timesheets --------------------------------------------------------------------


class MissingTimesheetsWidget(WidgetBase):
    scope: MissingScopeName | None = Field(
        description="`practice`: everyone (`time.read_all`); `team`: the people the caller "
        "manages and the caller; null when hidden"
    )
    week_start: dt.date = Field(description="The week listed")
    week_end: dt.date
    last_completed_week_start: dt.date = Field(description="The default week")
    count: int
    rows: list[MissingTimesheet] = Field(description="By name; the shape of `/timesheets/missing`")
    can_remind: bool = Field(
        description="The caller may send reminders (`POST /timesheets/reminders/run`)"
    )


# --- Top clients by WIP --------------------------------------------------------------------


class WipClient(BaseModel):
    client_id: uuid.UUID
    client_code: str
    client_name: str
    amount: int = Field(description="Time and disbursements, as at today")
    time_value: int
    disbursement_value: int
    minutes: int
    oldest_date: dt.date | None
    over_90_days: int = Field(description="The part of `amount` older than 90 days")
    share_bp: int | None = Field(description="Of the practice's WIP, basis points")
    previous_amount: int = Field(description="As at the day before the period started")
    change: int = Field(description="amount minus previous_amount")


class TopClientsByWip(WidgetBase):
    period: DashboardPeriod
    date_from: dt.date
    total: int = Field(description="The practice's WIP as at today")
    previous_total: int = Field(description="As at the day before the period started")
    client_count: int = Field(description="Clients with WIP")
    items: list[WipClient] = Field(description="Largest first, at most `limit`")
    others_amount: int = Field(description="WIP of the clients not listed")


# --- Billing trend -------------------------------------------------------------------------


class BillingMonth(BaseModel):
    month: dt.date = Field(description="The first day of the month")
    billed: int = Field(description="Net of invoices issued")
    invoice_count: int
    credit_notes: int = Field(description="Net of credit notes issued")
    credit_note_count: int
    net_billed: int = Field(description="billed minus credit_notes")
    receipts: int = Field(description="Payments received, not voided, money on account included")


class BillingWindow(BaseModel):
    date_from: dt.date
    date_to: dt.date
    billed: int
    credit_notes: int
    net_billed: int
    receipts: int


class BillingTrend(WidgetBase):
    months: int
    series: list[BillingMonth] = Field(description="Oldest first; every month, empty ones too")
    this_month: BillingWindow = Field(description="From the first of the month to today")
    last_month: BillingWindow = Field(description="The same number of days of the month before")
    billed_comparison: CardComparison = Field(
        description="`billed` of this month against the same days last month"
    )


# --- Debtor ageing -------------------------------------------------------------------------


class AgeingBucket(BaseModel):
    key: BucketKey = Field(
        description="Days past the due date: `current` is not yet due; `days_90_plus` is more "
        "than 90 days"
    )
    amount: int
    invoice_count: int
    share_bp: int | None = Field(description="Of the outstanding total, basis points")


class DashboardDebtorRow(BaseModel):
    client_id: uuid.UUID
    client_code: str
    client_name: str
    invoice_count: int
    total: int = Field(description="Outstanding")
    overdue: int = Field(description="Outstanding past the due date")
    oldest_due_date: dt.date | None = Field(
        description="The oldest due date among the overdue invoices; null when none is overdue"
    )
    current: int
    days_1_30: int
    days_31_60: int
    days_61_90: int
    days_90_plus: int


class DebtorAgeing(WidgetBase):
    period: DashboardPeriod
    date_from: dt.date
    total: int = Field(description="Outstanding balance of issued invoices, as at today")
    overdue_total: int
    previous_total: int = Field(description="As at the day before the period started")
    previous_overdue_total: int
    invoice_count: int = Field(description="Invoices with a balance")
    debtor_count: int = Field(description="Clients with a balance")
    buckets: list[AgeingBucket] = Field(description="The five age buckets, current first")
    top_debtors: list[DashboardDebtorRow] = Field(
        description="Largest overdue balance first (then largest balance), at most `limit`"
    )
