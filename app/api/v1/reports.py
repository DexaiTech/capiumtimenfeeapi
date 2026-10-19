"""Reports: debtors, utilisation, recoverability, billing, profitability, expenses, payments
and missing timesheets. HTTP only. (The WIP report is in ``wip_reports``.)

Every report answers ``{rows, totals, filters}`` in the envelope (the others with a ``page``), or a
file with ``?format=csv|xlsx|pdf``. The rules live in ``ReportService``.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.core.context import RequestContext
from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, require
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.reports import (
    AgedDebtorsQuery,
    AgedDebtorsReport,
    BillingSummaryQuery,
    BillingSummaryReport,
    ExportFormat,
    JobProfitabilityQuery,
    JobProfitabilityReport,
    RecoverabilityQuery,
    RecoverabilityReport,
    TimeSummaryQuery,
    TimeSummaryReport,
    UtilisationQuery,
    UtilisationReport,
    VatSummaryQuery,
    VatSummaryReport,
)
from app.schemas.reports_ext import (
    ExpensesQuery,
    ExpensesReport,
    MissingTimesheetsQuery,
    MissingTimesheetsReport,
    PaymentsQuery,
    PaymentsReport,
)
from app.services import report_csv
from app.services.policy import OrgContext
from app.services.report_export import ReportExporter
from app.services.reports import ReportService
from app.services.reports_ext import ExtendedReportService
from app.services.xlsx_export import XLSX_MEDIA_TYPE

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Reports"])


def get_report_service(session: SessionDep) -> ReportService:
    return ReportService(session)


def get_extended_report_service(session: SessionDep) -> ExtendedReportService:
    return ExtendedReportService(session)


def get_report_exporter(session: SessionDep) -> ReportExporter:
    return ReportExporter(session)


ReportServiceDep = Annotated[ReportService, Depends(get_report_service)]
ExtendedServiceDep = Annotated[ExtendedReportService, Depends(get_extended_report_service)]
ExporterDep = Annotated[ReportExporter, Depends(get_report_exporter)]
ReadDep = Annotated[OrgContext, Depends(require("report.read"))]
FinancialDep = Annotated[OrgContext, Depends(require("report.financial"))]

_COST_NOTE = " Cost and margin figures are null without `rate.read`."
_PAGING_NOTE = (
    " `limit` and `offset` page the rows (`page` says where the page sits; `totals` always "
    "cover the whole report)."
)
_EXPORT_NOTE = (
    " `?format=csv|xlsx|pdf` downloads every row as an attachment (UTF-8 CSV with a Total "
    "row, an Excel sheet, a PDF table); it needs `report.export` as well and is recorded in "
    "the audit log."
)
_FILE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **error_responses(403, 404),
    200: {
        "description": "The report; a file with `?format=csv`, `xlsx` or `pdf`",
        "content": {
            "text/csv": {"schema": {"type": "string"}},
            XLSX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}},
            "application/pdf": {"schema": {"type": "string", "format": "binary"}},
        },
    },
}


async def _deliver(
    name: str,
    report: report_csv.ReportLike,
    fmt: ExportFormat,
    org: OrgContext,
    ctx: RequestContext,
    exporter: ReportExporter,
) -> Any:
    """The report in the envelope, or as a download (audited)."""
    if fmt == "json":
        return ok(report)
    file = await exporter.export(org, ctx, name, report, fmt)
    return Response(
        content=file.content,
        media_type=file.media_type,
        headers={"Content-Disposition": f'attachment; filename="{file.filename}"'},
    )


@router.get(
    "/reports/aged-debtors",
    summary="Aged debtors",
    description=(
        "Outstanding balance of issued invoices per client as at the end of a day (gross "
        "less payments received and credit notes issued by then), bucketed by days past the "
        "due date: current (not yet due), 1-30, 31-60, 61-90, 91+. Void invoices are left "
        "out. The `days_90_plus` bucket is the spec's `90+`, the same figure as `days_91_plus`. "
        "Requires `report.financial`." + _PAGING_NOTE + _EXPORT_NOTE
    ),
    response_model=ApiResponse[AgedDebtorsReport],
    responses=_FILE_RESPONSES,
)
async def aged_debtors_report(
    org: FinancialDep,
    service: ReportServiceDep,
    exporter: ExporterDep,
    ctx: ContextDep,
    query: Annotated[AgedDebtorsQuery, Query()],
) -> Any:
    exporter.ensure_allowed(org, query.format)
    report = await service.aged_debtors(org, query)
    return await _deliver("aged-debtors", report, query.format, org, ctx, exporter)


@router.get(
    "/reports/time-summary",
    summary="Time summary",
    description=(
        "Minutes logged in the range, chargeable (billable) and non-chargeable, grouped by "
        "person, client, job, activity, service, day, week or month. Without `time.read_all` "
        "only the caller's own time is reported (another `user_id` answers 403). Requires "
        "`report.read`." + _COST_NOTE + _PAGING_NOTE + _EXPORT_NOTE
    ),
    response_model=ApiResponse[TimeSummaryReport],
    responses=_FILE_RESPONSES,
)
async def time_summary_report(
    org: ReadDep,
    service: ReportServiceDep,
    exporter: ExporterDep,
    ctx: ContextDep,
    query: Annotated[TimeSummaryQuery, Query()],
) -> Any:
    exporter.ensure_allowed(org, query.format)
    report = await service.time_summary(org, query)
    return await _deliver("time-summary", report, query.format, org, ctx, exporter)


@router.get(
    "/reports/utilisation",
    summary="Utilisation",
    description=(
        "Per person: capacity (weekly capacity x working days in the range / working days "
        "per week), total and chargeable minutes, utilisation (chargeable / capacity) against "
        "the person's chargeable target, in basis points. Without `time.read_all` only the "
        "caller's own row. Requires `report.read`." + _PAGING_NOTE + _EXPORT_NOTE
    ),
    response_model=ApiResponse[UtilisationReport],
    responses=_FILE_RESPONSES,
)
async def utilisation_report(
    org: ReadDep,
    service: ReportServiceDep,
    exporter: ExporterDep,
    ctx: ContextDep,
    query: Annotated[UtilisationQuery, Query()],
) -> Any:
    exporter.ensure_allowed(org, query.format)
    report = await service.utilisation(org, query)
    return await _deliver("utilisation", report, query.format, org, ctx, exporter)


@router.get(
    "/reports/recoverability",
    summary="Recoverability",
    description=(
        "Per client, job or person: WIP value billed and what it was billed at (live "
        "allocations to invoices issued in the range), write-offs dated in the range, and "
        "recovery (billed / WIP value) in basis points. Requires `report.financial`."
        + _PAGING_NOTE
        + _EXPORT_NOTE
    ),
    response_model=ApiResponse[RecoverabilityReport],
    responses=_FILE_RESPONSES,
)
async def recoverability_report(
    org: FinancialDep,
    service: ReportServiceDep,
    exporter: ExporterDep,
    ctx: ContextDep,
    query: Annotated[RecoverabilityQuery, Query()],
) -> Any:
    exporter.ensure_allowed(org, query.format)
    report = await service.recoverability(org, query)
    return await _deliver("recoverability", report, query.format, org, ctx, exporter)


@router.get(
    "/reports/billing-summary",
    summary="Billing summary",
    description=(
        "Invoices issued (net, VAT, gross), credit notes and receipts in the range, per "
        "month (every month of the range) or per partner (the client's partner). Void "
        "invoices are left out. Requires `report.financial`." + _PAGING_NOTE + _EXPORT_NOTE
    ),
    response_model=ApiResponse[BillingSummaryReport],
    responses=_FILE_RESPONSES,
)
async def billing_summary_report(
    org: FinancialDep,
    service: ReportServiceDep,
    exporter: ExporterDep,
    ctx: ContextDep,
    query: Annotated[BillingSummaryQuery, Query()],
) -> Any:
    exporter.ensure_allowed(org, query.format)
    report = await service.billing_summary(org, query)
    return await _deliver("billing-summary", report, query.format, org, ctx, exporter)


@router.get(
    "/reports/job-profitability",
    summary="Job profitability",
    description=(
        "Per job with time or billing in the range (default: all time): minutes and value "
        "logged, cost, billed value, write-offs, margin (billed time - cost) and budget use. "
        "Requires `report.financial`." + _COST_NOTE + _PAGING_NOTE + _EXPORT_NOTE
    ),
    response_model=ApiResponse[JobProfitabilityReport],
    responses=_FILE_RESPONSES,
)
async def job_profitability_report(
    org: FinancialDep,
    service: ReportServiceDep,
    exporter: ExporterDep,
    ctx: ContextDep,
    query: Annotated[JobProfitabilityQuery, Query()],
) -> Any:
    exporter.ensure_allowed(org, query.format)
    report = await service.job_profitability(org, query)
    return await _deliver("job-profitability", report, query.format, org, ctx, exporter)


@router.get(
    "/reports/vat-summary",
    summary="VAT summary",
    description=(
        "Output VAT by rate on invoices issued in the range minus credit notes issued in "
        "the range (invoice basis), from the rates snapshotted on the lines. Requires "
        "`report.financial`." + _PAGING_NOTE + _EXPORT_NOTE
    ),
    response_model=ApiResponse[VatSummaryReport],
    responses=_FILE_RESPONSES,
)
async def vat_summary_report(
    org: FinancialDep,
    service: ReportServiceDep,
    exporter: ExporterDep,
    ctx: ContextDep,
    query: Annotated[VatSummaryQuery, Query()],
) -> Any:
    exporter.ensure_allowed(org, query.format)
    report = await service.vat_summary(org, query)
    return await _deliver("vat-summary", report, query.format, org, ctx, exporter)


@router.get(
    "/reports/expenses",
    summary="Expenses",
    description=(
        "Expense lines incurred in the range (default: this month) grouped by expense `type`, "
        "claimant (`person`), `client`, `job`, `month` (every month of the range) or claim "
        "`status`: lines, net, VAT, gross, the net that is billable, what is recharged to "
        "clients, what has been invoiced and what is owed back to staff. Filters match "
        "`GET /expenses`: claim `status` (repeat), `client_id`, `job_id`, `user_id` (the "
        "claimant), `expense_type_id`, `is_billable`, `billing_status` (repeat). Requires "
        "`report.read` or `report.financial`; everyone's claims with `expense.read_all` or "
        "`expense.manage`, otherwise one's own (and, with `expense.approve`, those of the "
        "people one manages)." + _PAGING_NOTE + _EXPORT_NOTE
    ),
    response_model=ApiResponse[ExpensesReport],
    responses=_FILE_RESPONSES,
)
async def expenses_report(
    org: OrgContextDep,
    service: ExtendedServiceDep,
    exporter: ExporterDep,
    ctx: ContextDep,
    query: Annotated[ExpensesQuery, Query()],
) -> Any:
    exporter.ensure_allowed(org, query.format)
    report = await service.expenses(org, query)
    return await _deliver("expenses", report, query.format, org, ctx, exporter)


@router.get(
    "/reports/payments",
    summary="Payments",
    description=(
        "Payments received in the range (default: this month) grouped by `month` (every month "
        "of the range), `client` or payment `method`: how many, the amount received (including "
        "what is on account), the part allocated to invoices, the part on account, and the "
        "payments voided or reversed (left out of the received figures). `received` equals the "
        "`receipts` of the billing summary and the payments `GET /payments` lists. Requires "
        "`report.financial`." + _PAGING_NOTE + _EXPORT_NOTE
    ),
    response_model=ApiResponse[PaymentsReport],
    responses=_FILE_RESPONSES,
)
async def payments_report(
    org: FinancialDep,
    service: ExtendedServiceDep,
    exporter: ExporterDep,
    ctx: ContextDep,
    query: Annotated[PaymentsQuery, Query()],
) -> Any:
    exporter.ensure_allowed(org, query.format)
    report = await service.payments(org, query)
    return await _deliver("payments", report, query.format, org, ctx, exporter)


@router.get(
    "/reports/missing-timesheets",
    summary="Missing timesheets",
    description=(
        "Who has not submitted a timesheet, one row per person and week, for a range of weeks "
        "(`week_from` to `week_to`, any day of each week; default: the last completed week; "
        "at most 26 weeks, none that has not started): the timesheet's state (`none`, `open`, "
        "`rejected`), minutes logged and expected, the shortfall, the working days up to today "
        "with no time and when a reminder was last sent. Who is listed is decided as for "
        "`GET /timesheets/missing`: everyone with `time.read_all`, otherwise the people the "
        "caller manages (`user_id` and `manager_user_id` narrow it). Requires `report.read`."
        + _PAGING_NOTE
        + _EXPORT_NOTE
    ),
    response_model=ApiResponse[MissingTimesheetsReport],
    responses=_FILE_RESPONSES,
)
async def missing_timesheets_report(
    org: ReadDep,
    service: ExtendedServiceDep,
    exporter: ExporterDep,
    ctx: ContextDep,
    query: Annotated[MissingTimesheetsQuery, Query()],
) -> Any:
    exporter.ensure_allowed(org, query.format)
    report = await service.missing_timesheets(org, query)
    return await _deliver("missing-timesheets", report, query.format, org, ctx, exporter)
