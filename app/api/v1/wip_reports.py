"""The WIP report and its drill-down. HTTP only.

``GET /reports/wip`` is two reports in one route, told apart by ``group_by``:

* without it, the original report: one row per client and job with the original columns, JSON or
  CSV (the rules live in ``ReportService.wip``);
* with it (or with ``format=xlsx``), the historic report: rows by client, job or billing type with
  Time, Fixed fee and Expense WIP, write-ons and write-offs and the ages, as at a day whose figures
  do not change afterwards (``WipHistoryService``), as JSON, CSV or Excel.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.core.exceptions import ValidationError
from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import require
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.reports import WipQuery, WipReport
from app.schemas.wip_reports import (
    HistoricWipItem,
    HistoricWipItemsQuery,
    HistoricWipQuery,
    HistoricWipReport,
)
from app.services import report_csv
from app.services.policy import OrgContext
from app.services.reports import ReportService, practice_today
from app.services.wip_history import WipHistoryService
from app.services.wip_report_export import WipReportExporter
from app.services.xlsx_export import XLSX_MEDIA_TYPE
from app.utils.pagination import PageParams

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Reports"])


def get_report_service(session: SessionDep) -> ReportService:
    return ReportService(session)


def get_history_service(session: SessionDep) -> WipHistoryService:
    return WipHistoryService(session)


def get_exporter(session: SessionDep) -> WipReportExporter:
    return WipReportExporter(session)


ReportServiceDep = Annotated[ReportService, Depends(get_report_service)]
HistoryServiceDep = Annotated[WipHistoryService, Depends(get_history_service)]
ExporterDep = Annotated[WipReportExporter, Depends(get_exporter)]
FinancialDep = Annotated[OrgContext, Depends(require("report.financial"))]

_WIP_RESPONSES: dict[int | str, dict[str, Any]] = {
    **error_responses(403, 404),
    200: {
        "description": "The report; a CSV or Excel attachment with `?format=csv` or `xlsx`",
        "content": {
            "text/csv": {"schema": {"type": "string"}},
            XLSX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}},
        },
    },
}


def _original_response(report: Any, fmt: str) -> Any:
    """The original report in the envelope, or as the CSV it always was."""
    if fmt == "csv":
        return Response(
            content=report_csv.render_report("wip", report),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{report_csv.filename("wip", report)}"'
            },
        )
    return ok(report)


@router.get(
    "/reports/wip",
    summary="Work in progress",
    description=(
        "Unbilled work in progress as at a day. **With `group_by`** (`client`, `job` or "
        "`billing_type`; also when `format=xlsx`, then `job` unless given) each row has Time "
        "WIP, Fixed fee WIP and Expense WIP, the write-ons and write-offs posted up to the day "
        "(net of reversals), the total, the value in age buckets (0-30, 31-60, 61-90, 90+ "
        "days) and the oldest item date, with the same totals. The past is immutable: a day "
        "before today is frozen the first time it is reported (`snapshot.frozen`), so later "
        "invoices, write-offs or rate changes never change it; today is computed live. Drill "
        "down with `GET /reports/wip/items`. **Without `group_by`** the original report: one "
        "row per client and job (hours, time value, disbursements, total, ages) as JSON or "
        "CSV, as at the day by item date. A date after today is 422 in both. `format` is "
        "`json`, `csv` (UTF-8 with BOM, amounts in decimal pounds) or `xlsx`; a download of "
        "the historic report needs `report.export` as well and is audited. Filters: client, "
        "job, person (their time only), partner, manager, service. Requires "
        "`report.financial`."
    ),
    response_model=ApiResponse[HistoricWipReport] | ApiResponse[WipReport],
    responses=_WIP_RESPONSES,
)
async def wip_report(
    org: FinancialDep,
    reports: ReportServiceDep,
    history: HistoryServiceDep,
    exporter: ExporterDep,
    ctx: ContextDep,
    query: Annotated[HistoricWipQuery, Query()],
) -> Any:
    if query.as_at is not None and query.as_at > practice_today(org.organization):
        raise ValidationError.for_field(
            "as_at", "The date cannot be in the future.", code="future_date"
        )
    if query.group_by is None and query.format != "xlsx":
        original = await reports.wip(
            org,
            WipQuery(
                as_at=query.as_at,
                client_id=query.client_id,
                job_id=query.job_id,
                user_id=query.user_id,
                partner_user_id=query.partner_user_id,
                manager_user_id=query.manager_user_id,
                service_id=query.service_id,
                format="csv" if query.format == "csv" else "json",
            ),
        )
        return _original_response(original, query.format)
    exporter.ensure_allowed(org, query.format)
    report = await history.report(org, query)
    if query.format == "json":
        return ok(report)
    file = await exporter.export(org, ctx, report, query.format)
    return Response(
        content=file.content,
        media_type=file.media_type,
        headers={"Content-Disposition": f'attachment; filename="{file.filename}"'},
    )


@router.get(
    "/reports/wip/items",
    summary="The items behind the WIP report",
    description=(
        "The items of the historic WIP report as at a day, oldest first and "
        "cursor-paginated: time entries, disbursements, expense lines, the fixed-fee WIP of "
        "jobs and the adjustments and transfers in force, each with its value, age at that "
        "day, client, job and billing type. A closed day is read from its frozen snapshot, so "
        "the items always add up to the report's rows. Filters: client, job, billing type, "
        "kind (repeat the parameter), partner, manager, service. A future date is 422. "
        "Requires `report.financial`."
    ),
    response_model=ApiResponse[list[HistoricWipItem]],
    responses=error_responses(403, 404),
)
async def wip_report_items(
    org: FinancialDep,
    history: HistoryServiceDep,
    query: Annotated[HistoricWipItemsQuery, Query()],
) -> ApiResponse[list[HistoricWipItem]]:
    result = await history.items(org, query, PageParams(limit=query.limit, cursor=query.cursor))
    return ok_page(result.items, result.next_cursor)
