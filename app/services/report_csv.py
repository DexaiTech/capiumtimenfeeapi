"""CSV rendering of the reports, for Excel.

UTF-8 with a byte-order mark (so Excel detects the encoding), CRLF line endings, one
header row, the report's rows and a final "Total" row. Money is written as decimal
pounds (``123456`` -> ``1234.56``), minutes as decimal hours to 2 dp, basis points as a
percentage to 2 dp (``7500`` -> ``75.00``). Text that a spreadsheet would read as a
formula is prefixed with an apostrophe.
"""

import csv
import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from app.schemas.reports import ReportFilters

Kind = Literal["text", "money", "hours", "minutes", "int", "bp", "date"]
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True, slots=True)
class Column:
    key: str
    header: str
    kind: Kind = "money"


def money(minor: int) -> str:
    """Minor units as decimal pounds: ``-5`` -> ``-0.05``."""
    sign = "-" if minor < 0 else ""
    whole, pence = divmod(abs(minor), 100)
    return f"{sign}{whole}.{pence:02d}"


def hours(minutes: int) -> str:
    value = (Decimal(minutes) / 60).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{value:.2f}"


def percent(bp: int) -> str:
    return money(bp)  # basis points are hundredths of a percent, like pence of a pound


def _text(value: Any) -> str:
    text = str(value)
    return f"'{text}" if text.startswith(_FORMULA_START) else text


def _cell(value: Any, kind: Kind) -> str:
    if value is None:
        return ""
    match kind:
        case "money":
            return money(int(value))
        case "hours":
            return hours(int(value))
        case "bp":
            return percent(int(value))
        case "minutes" | "int":
            return str(int(value))
        case "date":
            if isinstance(value, datetime):
                return value.date().isoformat()
            return value.isoformat() if isinstance(value, date) else str(value)
        case _:
            return _text(value)


def render(columns: Sequence[Column], rows: Sequence[BaseModel], totals: BaseModel | None) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([c.header for c in columns])
    for row in rows:
        writer.writerow([_cell(getattr(row, c.key, None), c.kind) for c in columns])
    if totals is not None:
        cells = []
        for index, column in enumerate(columns):
            if index == 0:
                cells.append("Total")
            elif column.kind == "text" or column.kind == "date":
                cells.append("")
            else:
                cells.append(_cell(getattr(totals, column.key, None), column.kind))
        writer.writerow(cells)
    return buffer.getvalue().encode("utf-8-sig")


# --- column sets --------------------------------------------------------------------------

_WIP = (
    Column("client_code", "Client code", "text"),
    Column("client_name", "Client", "text"),
    Column("job_number", "Job number", "text"),
    Column("job_name", "Job", "text"),
    Column("minutes", "Hours", "hours"),
    Column("time_value", "Time value"),
    Column("disbursement_value", "Disbursements"),
    Column("total", "Total WIP"),
    Column("age_0_30", "0-30 days"),
    Column("age_31_60", "31-60 days"),
    Column("age_61_90", "61-90 days"),
    Column("age_91_plus", "91+ days"),
    Column("oldest_item_date", "Oldest item", "date"),
)
_AGED = (
    Column("client_code", "Client code", "text"),
    Column("client_name", "Client", "text"),
    Column("invoice_count", "Invoices", "int"),
    Column("current", "Current"),
    Column("days_1_30", "1-30 days"),
    Column("days_31_60", "31-60 days"),
    Column("days_61_90", "61-90 days"),
    Column("days_91_plus", "91+ days"),
    Column("total", "Total"),
    Column("oldest_due_date", "Oldest due date", "date"),
)
_TIME_MEASURES = (
    Column("entry_count", "Entries", "int"),
    Column("total_minutes", "Total hours", "hours"),
    Column("chargeable_minutes", "Chargeable hours", "hours"),
    Column("non_chargeable_minutes", "Non-chargeable hours", "hours"),
    Column("chargeable_bp", "Chargeable %", "bp"),
    Column("charge_value", "Charge value"),
    Column("cost_value", "Cost value"),
)
_GROUP_HEADERS = {
    "person": ("Initials", "Person"),
    "client": ("Client code", "Client"),
    "job": ("Job number", "Job"),
    "activity": ("Activity code", "Activity"),
    "service": ("Service code", "Service"),
    "day": (None, "Day"),
    "week": (None, "Week"),
    "month": (None, "Month"),
    "type": ("Type code", "Expense type"),
    "status": (None, "Claim status"),
    "method": (None, "Payment method"),
}
_UTILISATION = (
    Column("full_name", "Person", "text"),
    Column("initials", "Initials", "text"),
    Column("capacity_minutes", "Capacity hours", "hours"),
    Column("total_minutes", "Total hours", "hours"),
    Column("chargeable_minutes", "Chargeable hours", "hours"),
    Column("non_chargeable_minutes", "Non-chargeable hours", "hours"),
    Column("utilisation_bp", "Utilisation %", "bp"),
    Column("target_bp", "Target %", "bp"),
    Column("variance_bp", "Variance %", "bp"),
)
_RECOVERY_MEASURES = (
    Column("billed_wip_value", "WIP value billed"),
    Column("billed_value", "Billed value"),
    Column("billing_adjustment", "Write-up/down on billing"),
    Column("written_off_value", "Written off"),
    Column("wip_value", "Total WIP cleared"),
    Column("net_adjustment", "Net write-up/off"),
    Column("recovery_bp", "Recovery %", "bp"),
)
_BILLING_MEASURES = (
    Column("invoice_count", "Invoices", "int"),
    Column("invoiced_net", "Invoiced net"),
    Column("invoiced_vat", "Invoiced VAT"),
    Column("invoiced_gross", "Invoiced gross"),
    Column("credit_note_count", "Credit notes", "int"),
    Column("credited_net", "Credited net"),
    Column("credited_vat", "Credited VAT"),
    Column("credited_gross", "Credited gross"),
    Column("net_billed", "Net billed"),
    Column("receipts", "Receipts"),
)
_JOBS = (
    Column("job_number", "Job number", "text"),
    Column("job_name", "Job", "text"),
    Column("client_code", "Client code", "text"),
    Column("client_name", "Client", "text"),
    Column("status", "Status", "text"),
    Column("billing_basis", "Billing basis", "text"),
    Column("logged_minutes", "Hours logged", "hours"),
    Column("budget_minutes", "Budget hours", "hours"),
    Column("budget_minutes_used_bp", "Budget hours used %", "bp"),
    Column("time_value", "Time value"),
    Column("budget_fee", "Budget fee"),
    Column("budget_fee_used_bp", "Budget fee used %", "bp"),
    Column("cost_value", "Cost"),
    Column("billed_time_value", "Billed time"),
    Column("billed_disbursement_value", "Billed disbursements"),
    Column("billed_value", "Billed total"),
    Column("written_off_value", "Written off"),
    Column("margin", "Margin"),
    Column("margin_bp", "Margin %", "bp"),
)
_VAT = (
    Column("name", "VAT rate", "text"),
    Column("rate_bp", "Rate %", "bp"),
    Column("invoiced_net", "Invoiced net"),
    Column("invoiced_vat", "Invoiced VAT"),
    Column("credited_net", "Credited net"),
    Column("credited_vat", "Credited VAT"),
    Column("net", "Net"),
    Column("vat", "Output VAT"),
)
_EXPENSE_MEASURES = (
    Column("line_count", "Lines", "int"),
    Column("net", "Net"),
    Column("vat", "VAT"),
    Column("gross", "Gross"),
    Column("billable_net", "Billable net"),
    Column("recharge_amount", "Recharge"),
    Column("recharged_amount", "Recharged (invoiced)"),
    Column("reimbursable", "To reimburse"),
)
_PAYMENT_MEASURES = (
    Column("payment_count", "Payments", "int"),
    Column("received", "Received"),
    Column("allocated", "Allocated"),
    Column("on_account", "On account"),
    Column("void_count", "Void or reversed", "int"),
    Column("void_amount", "Void or reversed amount"),
)
_MISSING = (
    Column("name", "Person", "text"),
    Column("week_start", "Week starting", "date"),
    Column("status", "Timesheet", "text"),
    Column("logged_minutes", "Logged hours", "hours"),
    Column("expected_minutes", "Expected hours", "hours"),
    Column("shortfall_minutes", "Shortfall hours", "hours"),
    Column("missing_day_count", "Days with no time", "int"),
    Column("last_reminded_at", "Last reminded", "date"),
)


def _grouped(group_by: str | None, measures: Sequence[Column], client: bool) -> list[Column]:
    code_header, name_header = _GROUP_HEADERS.get(group_by or "", (None, "Group"))
    columns = [Column("name", name_header, "text")]
    if code_header:
        columns.append(Column("code", code_header, "text"))
    if client:
        columns.append(Column("client_name", "Client", "text"))
    return [*columns, *measures]


def _time_columns(group_by: str | None) -> list[Column]:
    return _grouped(group_by, _TIME_MEASURES, client=group_by == "job")


def _recovery_columns(group_by: str | None) -> list[Column]:
    return _grouped(group_by, _RECOVERY_MEASURES, client=group_by == "job")


def _billing_columns(group_by: str | None) -> list[Column]:
    first = "Partner" if group_by == "partner" else "Month"
    return [Column("name", first, "text"), *_BILLING_MEASURES]


def _expense_columns(group_by: str | None) -> list[Column]:
    return _grouped(group_by, _EXPENSE_MEASURES, client=group_by == "job")


def _payment_columns(group_by: str | None) -> list[Column]:
    return _grouped(group_by, _PAYMENT_MEASURES, client=False)


_COLUMNS: dict[str, Callable[[str | None], Sequence[Column]]] = {
    "wip": lambda _: _WIP,
    "aged-debtors": lambda _: _AGED,
    "time-summary": _time_columns,
    "utilisation": lambda _: _UTILISATION,
    "recoverability": _recovery_columns,
    "billing-summary": _billing_columns,
    "job-profitability": lambda _: _JOBS,
    "vat-summary": lambda _: _VAT,
    "expenses": _expense_columns,
    "payments": _payment_columns,
    "missing-timesheets": lambda _: _MISSING,
}


class ReportLike(Protocol):
    """What ``app.schemas.reports.Report`` provides."""

    @property
    def rows(self) -> Sequence[BaseModel]: ...

    @property
    def totals(self) -> BaseModel: ...

    @property
    def filters(self) -> ReportFilters: ...


def columns_of(name: str, report: ReportLike) -> Sequence[Column]:
    """The columns every export format of the report shows, in order."""
    return _COLUMNS[name](report.filters.group_by)


def render_report(name: str, report: ReportLike) -> bytes:
    """CSV of a ``Report`` (``rows``, ``totals``, ``filters``) by report name."""
    return render(columns_of(name, report), report.rows, report.totals)


def stem(name: str, report: ReportLike) -> str:
    """The file name of a download without its extension: the report and its date or range."""
    filters = report.filters
    if filters.as_at is not None:
        suffix = filters.as_at.isoformat()
    elif filters.date_from is not None and filters.date_to is not None:
        suffix = f"{filters.date_from.isoformat()}_{filters.date_to.isoformat()}"
    else:
        suffix = "all"
    return f"{name}_{suffix}"


def filename(name: str, report: ReportLike) -> str:
    return f"{stem(name, report)}.csv"
