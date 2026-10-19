"""Downloads of the reports as CSV, Excel and PDF, and the audit row each one leaves.

The columns are the ones ``report_csv`` defines, so the three formats show the same figures; every
row of the report is exported, whatever page the screen shows. A download needs ``report.export``
on top of the permission of the report itself; the audit row names the report, the format, the
filters and the number of rows, never the rows.
"""

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from fpdf import FPDF
from fpdf.enums import TableBordersLayout, XPos, YPos
from fpdf.fonts import FontFace
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.db.models import (
    ActivityCode,
    Client,
    ExpenseType,
    Job,
    OrganizationMember,
    Service,
    User,
)
from app.db.session import unit_of_work
from app.schemas.reports import ReportFilters
from app.services import audit, report_csv
from app.services.policy import OrgContext, ensure
from app.services.reports import practice_today
from app.services.xlsx_export import XLSX_MEDIA_TYPE, XlsxColumn, render_xlsx

type FileFormat = Literal["csv", "xlsx", "pdf"]

EXPORT_PERMISSION = "report.export"
TITLES = {
    "wip": "WIP",
    "aged-debtors": "Aged Debtors",
    "time-summary": "Time Analysis",
    "utilisation": "Utilisation",
    "recoverability": "Recovery",
    "billing-summary": "Billing",
    "job-profitability": "Job Profitability",
    "vat-summary": "VAT Summary",
    "expenses": "Expenses",
    "payments": "Payments",
    "missing-timesheets": "Missing Timesheets",
}
_EXTENSIONS: dict[str, str] = {"csv": "csv", "xlsx": "xlsx", "pdf": "pdf"}
_MEDIA_TYPES: dict[str, str] = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": XLSX_MEDIA_TYPE,
    "pdf": "application/pdf",
}
# People and things a filter can name, by the filter field and the label it gets in the notes.
_NAMED_FILTERS = (
    ("client_id", "Client"),
    ("job_id", "Job"),
    ("user_id", "Person"),
    ("partner_user_id", "Partner"),
    ("manager_user_id", "Manager"),
    ("service_id", "Service"),
    ("activity_code_id", "Activity"),
    ("expense_type_id", "Expense type"),
)
_LIST_FILTERS = (("status", "Status"), ("billing_status", "Billing status"))
_MARGIN = 12
_GREY = (110, 110, 120)
_HEAD_FILL = (237, 233, 247)
# Typographic characters outside Latin-1 (the core PDF fonts) and what replaces them.
_REPLACEMENTS = {
    chr(0x2013): "-",
    chr(0x2014): "-",
    chr(0x2212): "-",
    chr(0x2018): "'",
    chr(0x2019): "'",
    chr(0x201C): '"',
    chr(0x201D): '"',
    chr(0x2026): "...",
    chr(0x2022): "*",
    chr(0x00A0): " ",
    chr(0x20AC): "EUR ",
}


@dataclass(frozen=True, slots=True)
class ExportFile:
    content: bytes
    media_type: str
    filename: str


def ensure_can_export(ctx: OrgContext, fmt: str) -> None:
    """A download needs ``report.export``; the JSON answer only the report's own permission."""
    if fmt != "json":
        ensure(ctx, EXPORT_PERMISSION)


# --- PDF -----------------------------------------------------------------------------------


def latin1(text: str | None) -> str:
    """Text the core PDF fonts can draw: Latin-1 only."""
    if not text:
        return ""
    for old, new in _REPLACEMENTS.items():
        text = text.replace(old, new)
    return text.encode("latin-1", "replace").decode("latin-1")


def _money(pence: int) -> str:
    sign = "-" if pence < 0 else ""
    pounds, rest = divmod(abs(pence), 100)
    return f"{sign}{chr(0xA3)}{pounds:,}.{rest:02d}"


def _percent(bp: int) -> str:
    sign = "-" if bp < 0 else ""
    whole, rest = divmod(abs(bp), 100)
    return f"{sign}{whole}.{rest:02d}%"


def _pdf_cell(value: Any, kind: str) -> str:
    """A figure as a person reads it: UK dates, pounds, hours as h:mm."""
    if value is None:
        return ""
    match kind:
        case "money":
            return _money(int(value))
        case "hours":
            hours, minutes = divmod(abs(int(value)), 60)
            return f"{'-' if int(value) < 0 else ''}{hours}:{minutes:02d}"
        case "bp":
            return _percent(int(value))
        case "int" | "minutes":
            return f"{int(value):,}"
        case "date":
            day = value.date() if isinstance(value, datetime) else value
            return day.strftime("%d/%m/%Y") if isinstance(day, date) else str(day)
        case _:
            return latin1(str(value))


class _ReportPdf(FPDF):  # type: ignore[misc]
    def __init__(self, title: str, practice: str, generated_on: date, *, landscape: bool) -> None:
        super().__init__(orientation="L" if landscape else "P", unit="mm", format="A4")
        self.practice = latin1(practice)
        self.generated = generated_on.strftime("%d/%m/%Y")
        self.set_title(latin1(title))
        self.set_creator("Capium Time & Fees")
        self.set_compression(False)
        self.set_margins(_MARGIN, _MARGIN, _MARGIN)
        self.set_auto_page_break(auto=True, margin=14)
        self.alias_nb_pages()

    def header(self) -> None:
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*_GREY)
        self.cell(0, 5, self.practice, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(0, 0, 0)

    def footer(self) -> None:
        self.set_y(-10)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*_GREY)
        self.cell(0, 5, f"Generated {self.generated}", align="L")
        self.set_x(_MARGIN)
        self.cell(0, 5, f"Page {self.page_no()} of {{nb}}", align="R")
        self.set_text_color(0, 0, 0)


def _widths(
    headers: Sequence[str], body: Sequence[Sequence[str]], usable: float
) -> tuple[float, ...]:
    """Column widths in proportion to what the columns hold, adding up to ``usable``."""
    need: list[int] = []
    for index, header in enumerate(headers):
        longest_word = max((len(word) for word in header.split()), default=1)
        longest_cell = max((len(row[index]) for row in body), default=0)
        need.append(min(max(longest_cell, longest_word + 1, 6), 36))
    scale = usable / sum(need)
    return tuple(width * scale for width in need)


def render_pdf(
    *,
    title: str,
    practice: str,
    generated_on: date,
    notes: Sequence[str],
    columns: Sequence[report_csv.Column],
    rows: Sequence[BaseModel],
    totals: BaseModel | None,
) -> bytes:
    """A table with the title and the filters on top, page numbers, and the Total row last.
    Wide tables are printed landscape."""
    pdf = _ReportPdf(title, practice, generated_on, landscape=len(columns) > 7)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 8, latin1(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*_GREY)
    for note in notes:
        pdf.cell(0, 4.5, latin1(note), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    body = [
        [_pdf_cell(getattr(row, column.key, None), column.kind) for column in columns]
        for row in rows
    ]
    total_cells: list[str] = []
    if totals is not None:
        for index, column in enumerate(columns):
            if index == 0:
                total_cells.append("Total")
            elif column.kind in ("text", "date"):
                total_cells.append("")
            else:
                total_cells.append(_pdf_cell(getattr(totals, column.key, None), column.kind))
    size = 8.0 if len(columns) <= 9 else (7.0 if len(columns) <= 13 else 6.0)
    usable = pdf.w - 2 * _MARGIN
    headers = [column.header for column in columns]
    widths = _widths(headers, [*body, total_cells] if total_cells else body, usable)
    aligns = tuple("LEFT" if column.kind in ("text", "date") else "RIGHT" for column in columns)
    pdf.set_font("Helvetica", "", size)
    with pdf.table(
        col_widths=widths,
        width=usable,
        text_align=aligns,
        line_height=size * 0.45,
        borders_layout=TableBordersLayout.HORIZONTAL_LINES,
        headings_style=FontFace(emphasis="BOLD", fill_color=_HEAD_FILL),
        padding=0.8,
    ) as table:
        head = table.row()
        for header in headers:
            head.cell(latin1(header))
        for cells in body:
            line = table.row()
            for cell in cells:
                line.cell(cell)
        if total_cells:
            last = table.row()
            for cell in total_cells:
                last.cell(cell, style=FontFace(emphasis="BOLD"))
    if not rows:
        pdf.ln(3)
        pdf.set_font("Helvetica", "I", 9)
        pdf.cell(0, 5, "Nothing to report for these filters.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())


# --- Filters in words ----------------------------------------------------------------------


def _day(value: date) -> str:
    return value.strftime("%d/%m/%Y")


class ReportExporter:
    """Renders a report in a file format and records the download in the audit log."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def ensure_allowed(ctx: OrgContext, fmt: str) -> None:
        ensure_can_export(ctx, fmt)

    async def export(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        name: str,
        report: report_csv.ReportLike,
        fmt: FileFormat,
    ) -> ExportFile:
        """The file; the report must hold every row (the route runs it unpaged)."""
        ensure_can_export(ctx, fmt)
        title = TITLES[name]
        organization = ctx.organization
        notes = await self.describe_filters(ctx.organization_id, report.filters)
        columns = report_csv.columns_of(name, report)
        today = practice_today(organization)
        match fmt:
            case "csv":
                content = report_csv.render_report(name, report)
            case "xlsx":
                content = await asyncio.to_thread(
                    render_xlsx,
                    [XlsxColumn(c.key, c.header, c.kind) for c in columns],
                    report.rows,
                    sheet=title,
                    title=f"{title} - {organization.name}",
                    notes=notes,
                    totals=report.totals,
                )
            case _:
                content = await asyncio.to_thread(
                    render_pdf,
                    title=title,
                    practice=organization.name,
                    generated_on=today,
                    notes=notes,
                    columns=columns,
                    rows=report.rows,
                    totals=report.totals,
                )
        async with unit_of_work(self.session):
            await audit.record(
                self.session,
                "report.exported",
                ctx=rctx,
                actor_user_id=ctx.user.id,
                organization_id=ctx.organization_id,
                target_type="report",
                meta={
                    "report": name,
                    "format": fmt,
                    "filters": self._filters_meta(report.filters),
                    "row_count": len(report.rows),
                },
            )
        return ExportFile(
            content=content,
            media_type=_MEDIA_TYPES[fmt],
            filename=f"{report_csv.stem(name, report)}.{_EXTENSIONS[fmt]}",
        )

    @staticmethod
    def _filters_meta(filters: ReportFilters) -> dict[str, Any]:
        dumped = filters.model_dump(mode="json", exclude_none=True)
        return {key: value for key, value in dumped.items() if value not in ([], "")}

    async def describe_filters(
        self, organization_id: uuid.UUID, filters: ReportFilters
    ) -> list[str]:
        """The filters of a report as lines of text (names, not ids) for a file's header."""
        lines: list[str] = []
        if filters.as_at is not None:
            lines.append(f"As at: {_day(filters.as_at)}")
        weeks = (getattr(filters, "week_from", None), getattr(filters, "week_to", None))
        if weeks[0] is not None and weeks[1] is not None:
            lines.append(f"Weeks starting: {_day(weeks[0])} to {_day(weeks[1])}")
        elif filters.date_from is not None and filters.date_to is not None:
            lines.append(f"Period: {_day(filters.date_from)} to {_day(filters.date_to)}")
        elif filters.date_from is not None:
            lines.append(f"Period: from {_day(filters.date_from)}")
        elif filters.date_to is not None:
            lines.append(f"Period: up to {_day(filters.date_to)}")
        if filters.group_by:
            lines.append(f"Grouped by: {filters.group_by.replace('_', ' ')}")
        for field, label in _NAMED_FILTERS:
            value = getattr(filters, field, None)
            if value is not None:
                named = await self._name_of(organization_id, field, value)
                lines.append(f"{label}: {named}")
        for field, label in _LIST_FILTERS:
            values = getattr(filters, field, None)
            if values:
                lines.append(f"{label}: {', '.join(str(v).replace('_', ' ') for v in values)}")
        method = getattr(filters, "method", None)
        if method:
            lines.append(f"Method: {str(method).replace('_', ' ')}")
        billable = getattr(filters, "is_billable", None)
        if billable is not None:
            lines.append(f"Billable: {'yes' if billable else 'no'}")
        return lines

    async def _name_of(self, organization_id: uuid.UUID, field: str, value: uuid.UUID) -> str:
        """The name a filter id stands for, falling back to the id itself."""
        stmt: Any
        match field:
            case "client_id":
                stmt = select(Client.name, Client.code).where(
                    Client.organization_id == organization_id, Client.id == value
                )
            case "job_id":
                stmt = select(Job.name, Job.number).where(
                    Job.organization_id == organization_id, Job.id == value
                )
            case "service_id":
                stmt = select(Service.name, Service.code).where(
                    Service.organization_id == organization_id, Service.id == value
                )
            case "activity_code_id":
                stmt = select(ActivityCode.name, ActivityCode.code).where(
                    ActivityCode.organization_id == organization_id, ActivityCode.id == value
                )
            case "expense_type_id":
                stmt = select(ExpenseType.name, ExpenseType.code).where(
                    ExpenseType.organization_id == organization_id, ExpenseType.id == value
                )
            case _:
                stmt = (
                    select(User.full_name)
                    .join(OrganizationMember, OrganizationMember.user_id == User.id)
                    .where(OrganizationMember.organization_id == organization_id, User.id == value)
                )
        row = (await self.session.execute(stmt)).first()
        if row is None:
            return str(value)
        return f"{row[0]} ({row[1]})" if len(row) > 1 and row[1] else str(row[0])
