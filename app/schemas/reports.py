"""Query parameters and response models of the report endpoints.

Every report answers ``{rows, totals, filters}``. Money is integer minor units, durations
whole minutes, percentages basis points (``null`` when the denominator is zero). Cost and
margin figures are ``null`` for a caller without ``rate.read``.
"""

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import RequestModel

ReportFormat = Literal["json", "csv"]
# Every report but WIP also downloads as Excel and PDF.
ExportFormat = Literal["json", "csv", "xlsx", "pdf"]
TimeGroupBy = Literal["person", "client", "job", "activity", "service", "day", "week", "month"]
RecoverabilityGroupBy = Literal["client", "job", "person"]
BillingGroupBy = Literal["month", "partner"]

# A report covers at most ten years: enough for any practice, bounded for the database.
MAX_RANGE_DAYS = 3_660
MAX_PAGE_SIZE = 1_000

_FORMAT_DOC = "`json` (default) or `csv` (UTF-8 with BOM, for Excel)"
_EXPORT_DOC = (
    "`json` (default), or a download of every row: `csv` (UTF-8 with BOM), `xlsx` (Excel) "
    "or `pdf`. A download needs `report.export` and ignores `limit` and `offset`"
)
_CLIENT = Field(None, description="Only this client")
_JOB = Field(None, description="Only this job")
_USER = Field(None, description="Only this person's time (disbursements are left out)")
_PARTNER = Field(
    None, description="Only work whose responsible partner (job's, else client's) is this person"
)
_MANAGER = Field(
    None, description="Only work whose responsible manager (job's, else client's) is this person"
)
_SERVICE = Field(None, description="Only jobs of this service")


class PagedQuery(RequestModel):
    """Format and paging shared by every report except WIP."""

    format: ExportFormat = Field("json", description=_EXPORT_DOC)
    limit: int | None = Field(
        None,
        ge=1,
        le=MAX_PAGE_SIZE,
        description="Rows per page of the JSON answer. Default: every row. `totals` always "
        "cover the whole report",
    )
    offset: int = Field(
        0, ge=0, le=1_000_000, description="Rows to skip (with `limit`); `page.next_offset`"
    )


class RangeQuery(PagedQuery):
    date_from: date | None = Field(
        None, description="First day, inclusive. Default: the first of date_to's month"
    )
    date_to: date | None = Field(
        None, description="Last day, inclusive. Default: today in the practice time zone"
    )


class WipQuery(RequestModel):
    as_at: date | None = Field(
        None, description="Items dated on or before this day. Default: today (practice time zone)"
    )
    client_id: uuid.UUID | None = _CLIENT
    job_id: uuid.UUID | None = _JOB
    user_id: uuid.UUID | None = _USER
    partner_user_id: uuid.UUID | None = _PARTNER
    manager_user_id: uuid.UUID | None = _MANAGER
    service_id: uuid.UUID | None = _SERVICE
    format: ReportFormat = Field("json", description=_FORMAT_DOC)


class AgedDebtorsQuery(PagedQuery):
    as_at: date | None = Field(
        None, description="Balances as at the end of this day. Default: today (practice time zone)"
    )
    client_id: uuid.UUID | None = _CLIENT
    partner_user_id: uuid.UUID | None = Field(None, description="Only clients of this partner")
    manager_user_id: uuid.UUID | None = Field(None, description="Only clients of this manager")


class TimeSummaryQuery(RangeQuery):
    group_by: TimeGroupBy = Field("person", description="How rows are grouped")
    client_id: uuid.UUID | None = _CLIENT
    job_id: uuid.UUID | None = _JOB
    user_id: uuid.UUID | None = Field(None, description="Only this person's time")
    partner_user_id: uuid.UUID | None = _PARTNER
    manager_user_id: uuid.UUID | None = _MANAGER
    service_id: uuid.UUID | None = _SERVICE
    activity_code_id: uuid.UUID | None = Field(None, description="Only this activity code")


class UtilisationQuery(RangeQuery):
    user_id: uuid.UUID | None = Field(None, description="Only this person")
    manager_user_id: uuid.UUID | None = Field(
        None, description="Only people whose timesheet manager is this person"
    )


class RecoverabilityQuery(RangeQuery):
    group_by: RecoverabilityGroupBy = Field("client", description="How rows are grouped")
    client_id: uuid.UUID | None = _CLIENT
    job_id: uuid.UUID | None = _JOB
    user_id: uuid.UUID | None = _USER
    partner_user_id: uuid.UUID | None = _PARTNER
    manager_user_id: uuid.UUID | None = _MANAGER
    service_id: uuid.UUID | None = _SERVICE


class BillingSummaryQuery(RangeQuery):
    group_by: BillingGroupBy = Field(
        "month", description="`month` (every month of the range) or `partner` (client's partner)"
    )
    client_id: uuid.UUID | None = _CLIENT
    partner_user_id: uuid.UUID | None = Field(None, description="Only clients of this partner")
    manager_user_id: uuid.UUID | None = Field(None, description="Only clients of this manager")


class JobProfitabilityQuery(PagedQuery):
    date_from: date | None = Field(
        None, description="Time logged and invoices issued from this day. Default: no limit"
    )
    date_to: date | None = Field(
        None, description="Time logged and invoices issued up to this day. Default: no limit"
    )
    client_id: uuid.UUID | None = _CLIENT
    job_id: uuid.UUID | None = _JOB
    partner_user_id: uuid.UUID | None = _PARTNER
    manager_user_id: uuid.UUID | None = _MANAGER
    service_id: uuid.UUID | None = _SERVICE


class VatSummaryQuery(RangeQuery):
    client_id: uuid.UUID | None = _CLIENT


class ReportFilters(BaseModel):
    """The filters the report was run with, defaults resolved."""

    date_from: date | None = None
    date_to: date | None = None
    as_at: date | None = None
    group_by: str | None = None
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    partner_user_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    service_id: uuid.UUID | None = None
    activity_code_id: uuid.UUID | None = None


class Report[RowT: BaseModel, TotalsT: BaseModel](BaseModel):
    rows: list[RowT]
    totals: TotalsT
    filters: ReportFilters


class ReportPage(BaseModel):
    """Which window of the report's rows the answer holds. ``totals`` cover all of them."""

    total: int = Field(description="Rows of the whole report, before `limit` and `offset`")
    limit: int | None = Field(description="The `limit` asked for; null: every row is returned")
    offset: int
    has_more: bool
    next_offset: int | None = Field(description="`offset` of the next page; null on the last")


class PagedReport[RowT: BaseModel, TotalsT: BaseModel](Report[RowT, TotalsT]):
    page: ReportPage


# --- WIP ---------------------------------------------------------------------------------


class WipAmounts(BaseModel):
    minutes: int
    time_value: int
    disbursement_value: int
    total: int
    age_0_30: int = Field(description="Value of items 0-30 days old (by item date)")
    age_31_60: int
    age_61_90: int
    age_91_plus: int


class WipRow(WipAmounts):
    client_id: uuid.UUID
    client_code: str
    client_name: str
    job_id: uuid.UUID | None
    job_number: str | None
    job_name: str | None
    oldest_item_date: date


class WipTotals(WipAmounts):
    oldest_item_date: date | None


# --- Aged debtors ------------------------------------------------------------------------


class AgedAmounts(BaseModel):
    invoice_count: int
    current: int = Field(description="Not yet due as at the date")
    days_1_30: int = Field(description="1-30 days past the due date")
    days_31_60: int
    days_61_90: int
    days_91_plus: int
    days_90_plus: int = Field(
        description="The spec's `90+` bucket: the same figure as days_91_plus"
    )
    total: int


class AgedDebtorRow(AgedAmounts):
    client_id: uuid.UUID
    client_code: str
    client_name: str
    oldest_due_date: date


class AgedDebtorTotals(AgedAmounts):
    oldest_due_date: date | None


# --- Time summary ------------------------------------------------------------------------


class TimeAmounts(BaseModel):
    entry_count: int
    total_minutes: int
    chargeable_minutes: int = Field(description="Minutes of billable entries")
    non_chargeable_minutes: int
    chargeable_bp: int | None = Field(description="Chargeable share of total, basis points")
    charge_value: int = Field(description="Charge-out value of the chargeable time")
    cost_value: int | None = Field(description="Cost of all the time; null without rate.read")


class TimeSummaryRow(TimeAmounts):
    key: str | None = Field(
        description="Id of the person/client/job/activity/service, or the ISO date that starts "
        "the day/week/month; null for time without one (e.g. internal time has no client)"
    )
    code: str | None = Field(description="Client code, job number, initials, activity code")
    name: str = Field(description="Display label of the group")
    client_name: str | None = Field(description="The job's client (group_by=job only)")
    user_id: uuid.UUID | None = Field(None, description="group_by=person: the person")
    client_id: uuid.UUID | None = Field(None, description="group_by=client or job: the client")
    job_id: uuid.UUID | None = Field(None, description="group_by=job: the job")
    activity_code_id: uuid.UUID | None = Field(None, description="group_by=activity")
    service_id: uuid.UUID | None = Field(None, description="group_by=service")
    date_from: date | None = Field(
        None, description="group_by=day, week or month: first day of the row, inside the range"
    )
    date_to: date | None = Field(
        None, description="group_by=day, week or month: last day of the row, inside the range"
    )


class TimeSummaryTotals(TimeAmounts):
    pass


# --- Utilisation -------------------------------------------------------------------------


class UtilisationAmounts(BaseModel):
    capacity_minutes: int = Field(
        description="Weekly capacity x working days in range / working days per week"
    )
    total_minutes: int
    chargeable_minutes: int
    non_chargeable_minutes: int
    utilisation_bp: int | None = Field(description="Chargeable / capacity, basis points")
    target_bp: int | None = Field(description="Chargeable target, basis points")
    variance_bp: int | None = Field(description="utilisation_bp - target_bp")


class UtilisationRow(UtilisationAmounts):
    user_id: uuid.UUID
    full_name: str
    initials: str | None
    status: str


class UtilisationTotals(UtilisationAmounts):
    working_days: int = Field(description="Working days of the practice in the range")


# --- Recoverability ----------------------------------------------------------------------


class RecoveryAmounts(BaseModel):
    billed_wip_value: int = Field(description="WIP value of the items cleared by invoices")
    billed_value: int = Field(description="What those items were billed at")
    billing_adjustment: int = Field(
        description="billed_value - billed_wip_value: write-up (+) or write-down (-) on billing"
    )
    written_off_value: int = Field(description="WIP value written off without an invoice")
    wip_value: int = Field(description="billed_wip_value + written_off_value")
    net_adjustment: int = Field(description="billed_value - wip_value")
    recovery_bp: int | None = Field(description="billed_value / wip_value, basis points")


class RecoverabilityRow(RecoveryAmounts):
    key: str | None = Field(description="Client, job or person id (null: no job / no person)")
    code: str | None
    name: str
    client_name: str | None = Field(description="The job's client (group_by=job only)")
    user_id: uuid.UUID | None = Field(None, description="group_by=person: the person")
    client_id: uuid.UUID | None = Field(None, description="group_by=client or job: the client")
    job_id: uuid.UUID | None = Field(None, description="group_by=job: the job")


class RecoverabilityTotals(RecoveryAmounts):
    pass


# --- Billing summary ---------------------------------------------------------------------


class BillingAmounts(BaseModel):
    invoice_count: int
    invoiced_net: int
    invoiced_vat: int
    invoiced_gross: int
    credit_note_count: int
    credited_net: int
    credited_vat: int
    credited_gross: int
    net_billed: int = Field(description="invoiced_net - credited_net")
    receipts: int = Field(description="Payments received (not voided)")


class BillingSummaryRow(BillingAmounts):
    key: str | None = Field(description="Month (YYYY-MM) or the partner's user id")
    name: str
    partner_user_id: uuid.UUID | None = Field(None, description="group_by=partner: the partner")
    date_from: date | None = Field(
        None, description="group_by=month: first day of the month, inside the range"
    )
    date_to: date | None = Field(
        None, description="group_by=month: last day of the month, inside the range"
    )


class BillingSummaryTotals(BillingAmounts):
    pass


# --- Job profitability -------------------------------------------------------------------


class JobProfitAmounts(BaseModel):
    logged_minutes: int
    time_value: int = Field(description="Charge-out value of the billable time logged")
    cost_value: int | None = Field(description="Cost of the time logged; null without rate.read")
    billed_time_value: int = Field(description="Billed value of the job's time")
    billed_disbursement_value: int
    billed_value: int
    written_off_value: int
    margin: int | None = Field(description="billed_time_value - cost_value")
    margin_bp: int | None = Field(description="margin / billed_time_value")


class JobProfitabilityRow(JobProfitAmounts):
    job_id: uuid.UUID
    job_number: str
    job_name: str
    status: str
    billing_basis: str
    client_id: uuid.UUID
    client_code: str
    client_name: str
    fixed_fee: int | None
    budget_minutes: int | None
    budget_minutes_used_bp: int | None = Field(description="logged_minutes / budget_minutes")
    budget_fee: int | None
    budget_fee_used_bp: int | None = Field(description="time_value / budget_fee")


class JobProfitabilityTotals(JobProfitAmounts):
    pass


# --- VAT summary -------------------------------------------------------------------------


class VatAmounts(BaseModel):
    invoiced_net: int
    invoiced_vat: int
    credited_net: int
    credited_vat: int
    net: int = Field(description="invoiced_net - credited_net")
    vat: int = Field(description="Output VAT: invoiced_vat - credited_vat")


class VatSummaryRow(VatAmounts):
    vat_rate_id: uuid.UUID | None
    name: str
    code: str | None
    rate_bp: int


class VatSummaryTotals(VatAmounts):
    pass


WipReport = Report[WipRow, WipTotals]
AgedDebtorsReport = PagedReport[AgedDebtorRow, AgedDebtorTotals]
TimeSummaryReport = PagedReport[TimeSummaryRow, TimeSummaryTotals]
UtilisationReport = PagedReport[UtilisationRow, UtilisationTotals]
RecoverabilityReport = PagedReport[RecoverabilityRow, RecoverabilityTotals]
BillingSummaryReport = PagedReport[BillingSummaryRow, BillingSummaryTotals]
JobProfitabilityReport = PagedReport[JobProfitabilityRow, JobProfitabilityTotals]
VatSummaryReport = PagedReport[VatSummaryRow, VatSummaryTotals]
