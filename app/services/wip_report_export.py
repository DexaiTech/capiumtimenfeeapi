"""The historic WIP report as a CSV or Excel download, and the audit row each download leaves.

Same conventions as the other reports (``report_export``): UTF-8 CSV with a header row and a Total
row, an Excel sheet with the practice and the filters on top, and a ``report.exported`` entry in the
audit log that names the report, the format, the filters and the number of rows (never the rows).
"""

import asyncio
from collections.abc import Sequence
from datetime import date
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.db.session import unit_of_work
from app.schemas.reports import ReportFilters
from app.schemas.wip_reports import (
    HistoricGroupBy,
    HistoricWipAmounts,
    HistoricWipReport,
    HistoricWipRow,
)
from app.services import audit, report_csv
from app.services.policy import OrgContext
from app.services.report_export import ExportFile, ReportExporter, ensure_can_export
from app.services.xlsx_export import XLSX_MEDIA_TYPE, XlsxColumn, render_xlsx

type FileFormat = Literal["csv", "xlsx"]

_MEDIA_TYPES: dict[str, str] = {"csv": "text/csv; charset=utf-8", "xlsx": XLSX_MEDIA_TYPE}
_TITLE = "WIP"
_HEADS: dict[str, tuple[report_csv.Column, ...]] = {
    "client": (
        report_csv.Column("code", "Client code", "text"),
        report_csv.Column("name", "Client", "text"),
    ),
    "job": (
        report_csv.Column("code", "Job number", "text"),
        report_csv.Column("name", "Job", "text"),
        report_csv.Column("client_name", "Client", "text"),
        report_csv.Column("billing_type", "Billing type", "text"),
    ),
    "billing_type": (report_csv.Column("name", "Billing type", "text"),),
}
_FIGURES: tuple[report_csv.Column, ...] = (
    report_csv.Column("minutes", "Hours", "hours"),
    report_csv.Column("time_wip", "Time WIP"),
    report_csv.Column("fixed_fee_wip", "Fixed fee WIP"),
    report_csv.Column("expense_wip", "Expense WIP"),
    report_csv.Column("write_ons", "Write-ons"),
    report_csv.Column("write_offs", "Write-offs"),
    report_csv.Column("total", "Total WIP"),
    report_csv.Column("age_0_30", "0-30 days"),
    report_csv.Column("age_31_60", "31-60 days"),
    report_csv.Column("age_61_90", "61-90 days"),
    report_csv.Column("age_90_plus", "90+ days"),
    report_csv.Column("oldest_date", "Oldest item", "date"),
    report_csv.Column("item_count", "Items", "int"),
)


class ExportRow(BaseModel):
    """A row of the report with its ages and names flattened, as the columns read them."""

    code: str | None = None
    name: str | None = None
    client_name: str | None = None
    billing_type: str | None = None
    minutes: int
    time_wip: int
    fixed_fee_wip: int
    expense_wip: int
    write_ons: int
    write_offs: int
    total: int
    age_0_30: int
    age_31_60: int
    age_61_90: int
    age_90_plus: int
    oldest_date: date | None
    item_count: int


def _flat(
    amounts: HistoricWipAmounts,
    code: str | None = None,
    name: str | None = None,
    client_name: str | None = None,
    billing_type: str | None = None,
) -> ExportRow:
    return ExportRow(
        code=code,
        name=name,
        client_name=client_name,
        billing_type=billing_type,
        minutes=amounts.minutes,
        time_wip=amounts.time_wip,
        fixed_fee_wip=amounts.fixed_fee_wip,
        expense_wip=amounts.expense_wip,
        write_ons=amounts.write_ons,
        write_offs=amounts.write_offs,
        total=amounts.total,
        age_0_30=amounts.ages.days_0_30,
        age_31_60=amounts.ages.days_31_60,
        age_61_90=amounts.ages.days_61_90,
        age_90_plus=amounts.ages.days_90_plus,
        oldest_date=amounts.oldest_date,
        item_count=amounts.item_count,
    )


def columns_of(group_by: HistoricGroupBy) -> list[report_csv.Column]:
    return [*_HEADS[group_by], *_FIGURES]


def flat_rows(rows: Sequence[HistoricWipRow]) -> list[ExportRow]:
    return [
        _flat(
            row,
            code=row.code,
            name=row.name,
            client_name=row.client.name if row.client else None,
            billing_type=row.billing_type,
        )
        for row in rows
    ]


def filename_of(report: HistoricWipReport, fmt: str) -> str:
    return f"wip_{report.filters.group_by}_{report.filters.as_at.isoformat()}.{fmt}"


class WipReportExporter:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def ensure_allowed(ctx: OrgContext, fmt: str) -> None:
        ensure_can_export(ctx, fmt)

    async def export(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        report: HistoricWipReport,
        fmt: FileFormat,
    ) -> ExportFile:
        ensure_can_export(ctx, fmt)
        columns = columns_of(report.filters.group_by)
        rows = flat_rows(report.rows)
        totals = _flat(report.totals)
        filters = report.filters
        notes: list[str] = []
        if fmt == "xlsx":
            notes = await ReportExporter(self.session).describe_filters(
                ctx.organization_id, ReportFilters.model_validate(filters.model_dump())
            )
            notes.append(report.notice)
            content = await asyncio.to_thread(
                render_xlsx,
                [XlsxColumn(c.key, c.header, c.kind) for c in columns],
                rows,
                sheet=_TITLE,
                title=f"{_TITLE} as at {filters.as_at.strftime('%d/%m/%Y')} - "
                f"{ctx.organization.name}",
                notes=notes,
                totals=totals,
            )
        else:
            content = report_csv.render(columns, rows, totals)
        async with unit_of_work(self.session):
            await audit.record(
                self.session,
                "report.exported",
                ctx=rctx,
                actor_user_id=ctx.user.id,
                organization_id=ctx.organization_id,
                target_type="report",
                meta={
                    "report": "wip",
                    "format": fmt,
                    "filters": filters.model_dump(mode="json", exclude_none=True),
                    "row_count": len(report.rows),
                },
            )
        return ExportFile(
            content=content, media_type=_MEDIA_TYPES[fmt], filename=filename_of(report, fmt)
        )
