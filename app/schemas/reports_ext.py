"""Query parameters and response models of the expenses, payments and missing-timesheets reports.

They follow the contract of the other reports (``{rows, totals, filters, page}``): money is integer
pence, durations whole minutes, ``totals`` cover every row of the report and ``page`` says which
window of the rows ``limit`` and ``offset`` returned.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.billing import BillingStatus
from app.schemas.expenses import ClaimStatus
from app.schemas.payments import PaymentMethod
from app.schemas.reports import PagedQuery, PagedReport, RangeQuery, ReportFilters
from app.schemas.time_views import MissingStatus

ExpenseGroupBy = Literal["type", "person", "client", "job", "month", "status"]
PaymentGroupBy = Literal["month", "client", "method"]

# The weeks one missing-timesheets report may cover: each one is a pass over the practice's staff.
MAX_MISSING_WEEKS = 26


# --- Expenses ----------------------------------------------------------------------------


class ExpensesQuery(RangeQuery):
    group_by: ExpenseGroupBy = Field(
        "type",
        description="How rows are grouped: expense type, claimant, client, job, month (every "
        "month of the range) or claim status",
    )
    client_id: uuid.UUID | None = Field(None, description="Only lines recharged to this client")
    job_id: uuid.UUID | None = Field(None, description="Only lines recharged to this job")
    user_id: uuid.UUID | None = Field(
        None,
        description="Only this person's claims. Without `expense.read_all` or `expense.manage` "
        "only one's own claims (and, with `expense.approve`, those of the people one manages) "
        "can be asked for (403 otherwise)",
    )
    expense_type_id: uuid.UUID | None = Field(None, description="Only lines of this expense type")
    status: list[ClaimStatus] = Field(
        default_factory=list,
        description="Only lines of claims in these statuses (repeat the parameter). "
        "Default: every status",
    )
    is_billable: bool | None = Field(
        None, description="Only lines that are (true) or are not (false) billable to a client"
    )
    billing_status: list[BillingStatus] = Field(
        default_factory=list,
        description="Only lines with these billing statuses (repeat the parameter)",
    )


class ExpenseAmounts(BaseModel):
    line_count: int
    net: int
    vat: int
    gross: int
    billable_net: int = Field(description="Net of the lines that are billable to a client")
    recharge_amount: int = Field(
        description="What the clients are charged for the billable lines: net plus markup, "
        "or the amount a manager set"
    )
    recharged_amount: int = Field(
        description="The part of recharge_amount that has been invoiced (billing status billed)"
    )
    reimbursable: int = Field(description="Gross of the lines the claimant paid (payer staff)")


class ExpenseRow(ExpenseAmounts):
    key: str | None = Field(
        description="Id of the type/person/client/job, the month (YYYY-MM) or the claim status; "
        "null for lines without a client or job"
    )
    code: str | None = Field(description="Type code, initials, client code or job number")
    name: str = Field(description="Display label of the group")
    client_name: str | None = Field(description="The job's client (group_by=job only)")
    expense_type_id: uuid.UUID | None = Field(None, description="group_by=type")
    user_id: uuid.UUID | None = Field(None, description="group_by=person: the claimant")
    client_id: uuid.UUID | None = Field(None, description="group_by=client or job: the client")
    job_id: uuid.UUID | None = Field(None, description="group_by=job")
    claim_status: ClaimStatus | None = Field(None, description="group_by=status")
    date_from: date | None = Field(
        None, description="group_by=month: first day of the month, inside the range"
    )
    date_to: date | None = Field(
        None, description="group_by=month: last day of the month, inside the range"
    )


class ExpenseTotals(ExpenseAmounts):
    pass


class ExpensesFilters(ReportFilters):
    expense_type_id: uuid.UUID | None = None
    status: list[str] = Field(default_factory=list)
    is_billable: bool | None = None
    billing_status: list[str] = Field(default_factory=list)


class ExpensesReport(PagedReport[ExpenseRow, ExpenseTotals]):
    filters: ExpensesFilters


# --- Payments ----------------------------------------------------------------------------


class PaymentsQuery(RangeQuery):
    group_by: PaymentGroupBy = Field(
        "month", description="`month` (every month of the range), `client` or payment `method`"
    )
    client_id: uuid.UUID | None = Field(None, description="Only this client's payments")
    method: PaymentMethod | None = Field(None, description="Only payments made this way")
    partner_user_id: uuid.UUID | None = Field(None, description="Only clients of this partner")
    manager_user_id: uuid.UUID | None = Field(None, description="Only clients of this manager")


class PaymentAmounts(BaseModel):
    payment_count: int = Field(description="Payments received (not void, not reversed)")
    received: int = Field(description="Their amount, including what is still on account")
    allocated: int = Field(description="The part allocated to invoices, as it stands now")
    on_account: int = Field(description="received - allocated: money not yet allocated")
    void_count: int = Field(
        description="Payments received in the range that were voided or reversed"
    )
    void_amount: int = Field(description="Their amount: not part of received")


class PaymentRow(PaymentAmounts):
    key: str | None = Field(description="Month (YYYY-MM), the client's id or the payment method")
    code: str | None = Field(description="Client code (group_by=client)")
    name: str = Field(description="Display label of the group")
    client_id: uuid.UUID | None = Field(None, description="group_by=client")
    method: PaymentMethod | None = Field(None, description="group_by=method")
    date_from: date | None = Field(
        None, description="group_by=month: first day of the month, inside the range"
    )
    date_to: date | None = Field(
        None, description="group_by=month: last day of the month, inside the range"
    )


class PaymentTotals(PaymentAmounts):
    pass


class PaymentsFilters(ReportFilters):
    method: str | None = None


class PaymentsReport(PagedReport[PaymentRow, PaymentTotals]):
    filters: PaymentsFilters


# --- Missing timesheets ------------------------------------------------------------------


class MissingTimesheetsQuery(PagedQuery):
    week_from: date | None = Field(
        None,
        description="Any day of the first week. Default: the last completed week (or `week_to`'s "
        "week when only that is given)",
    )
    week_to: date | None = Field(
        None,
        description="Any day of the last week; not later than this week. Default: the last "
        "completed week",
    )
    user_id: uuid.UUID | None = Field(
        None,
        description="Only this person. Without `time.read_all` only people the caller manages "
        "(403 for anyone else; one's own status is one's own to see)",
    )
    manager_user_id: uuid.UUID | None = Field(
        None, description="Only the people whose timesheets this manager approves"
    )
    status: list[MissingStatus] = Field(
        default_factory=list,
        description="Only timesheets in these states (repeat the parameter): `none` (no "
        "timesheet), `open` (not submitted), `rejected` (returned). Default: all",
    )


class MissingTimesheetRow(BaseModel):
    user_id: uuid.UUID
    name: str
    week_start: date
    status: MissingStatus = Field(
        description="none: no timesheet for the week; open: not submitted; rejected: returned"
    )
    timesheet_id: uuid.UUID | None
    logged_minutes: int
    expected_minutes: int = Field(description="The person's target for the week")
    shortfall_minutes: int = Field(description="expected_minutes - logged_minutes, at least 0")
    missing_day_count: int = Field(description="Working days up to today with no time logged")
    missing_days: list[date]
    last_reminded_at: datetime | None = Field(
        description="When a timesheet reminder for this week was last sent"
    )
    has_login: bool = Field(description="False: nobody can sign in as them, so no reminder")


class MissingTimesheetsTotals(BaseModel):
    timesheets: int = Field(description="Rows: a person's missing week counts once")
    people: int = Field(description="Different people in the rows")
    weeks: int = Field(description="Weeks the report covers")
    none_count: int
    open_count: int
    rejected_count: int
    logged_minutes: int
    expected_minutes: int
    shortfall_minutes: int
    missing_day_count: int


class MissingTimesheetsFilters(ReportFilters):
    week_from: date | None = None
    week_to: date | None = None
    status: list[str] = Field(default_factory=list)


class MissingTimesheetsReport(PagedReport[MissingTimesheetRow, MissingTimesheetsTotals]):
    filters: MissingTimesheetsFilters
