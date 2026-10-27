"""Bulk CSV import and export of staff, rates and jobs. HTTP only: the rules live in the
import and export services (docs/modules/import-export.md).

Every operation needs the practice permission ``data.import_export`` and the permission of
the data it touches. Registered before the staff, rates and jobs routers: ``/staff/export``,
``/rates/export`` and ``/jobs/export`` must not be read as an id.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import require
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.imports import BulkImportResult
from app.services.import_csv import MAX_UPLOAD_BODY_BYTES, ImportTooLargeError
from app.services.job_export import JobExportService
from app.services.job_import import JobImportService
from app.services.policy import OrgContext
from app.services.rate_export import RateExportService
from app.services.rate_import import RateImportService
from app.services.staff_export import StaffExportService
from app.services.staff_import import StaffImportService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Import & export"])

StaffExporter = Annotated[OrgContext, Depends(require("data.import_export", "member.read"))]
StaffImporter = Annotated[OrgContext, Depends(require("data.import_export", "member.invite"))]
RateExporter = Annotated[OrgContext, Depends(require("data.import_export", "rate.read"))]
RateImporter = Annotated[OrgContext, Depends(require("data.import_export", "rate.manage"))]
JobExporter = Annotated[OrgContext, Depends(require("data.import_export", "job.read"))]
JobImporter = Annotated[OrgContext, Depends(require("data.import_export", "job.create"))]
DryRun = Annotated[bool, Query(description="Validate only; write nothing")]

_CSV_RESPONSES = {
    **error_responses(403, 404),
    200: {"content": {"text/csv": {}}, "description": "CSV file"},
}
_UPLOAD = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {"file": {"type": "string", "format": "binary"}},
                    "required": ["file"],
                }
            },
            "text/csv": {"schema": {"type": "string"}},
        },
    }
}
_UPLOAD_ERRORS = (
    "413 `FILE_TOO_LARGE`, 415 `FILE_TYPE_NOT_ALLOWED`, 422 `IMPORT_TOO_MANY_ROWS` / "
    "`IMPORT_MISSING_COLUMNS` / `IMPORT_DUPLICATE_COLUMNS`, 400 `FILE_INVALID`."
)
_UPLOAD_COMMON = (
    "Upload a CSV (multipart/form-data field `file`, or a `text/csv` body; at most 5 MB and "
    "5000 rows). The header row is mapped by name (case and spaces are ignored, common "
    "aliases work) and unknown columns are ignored and reported. Dates are `YYYY-MM-DD` or "
    "`DD/MM/YYYY`, money is in pounds (`48.00`). `dry_run=true` validates and returns per-row "
    "errors without writing. A real run is all-or-nothing: 422 `IMPORT_INVALID` (errors in "
    "`details`) or every row is written in one transaction. "
)


def _csv(content: bytes, filename: str) -> Response:
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _read_body(request: Request) -> bytes:
    """The raw request body, refusing more than the upload cap (the global body-size
    middleware exempts ``/imports/...``)."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BODY_BYTES:
        raise ImportTooLargeError()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD_BODY_BYTES:
            raise ImportTooLargeError()
    return bytes(body)


# --- Staff -------------------------------------------------------------------------------


@router.get(
    "/staff/export",
    summary="Export staff as CSV",
    description=(
        "Every staff member of any status, ordered by code, as CSV (UTF-8 with BOM): `code`, "
        "`full_name`, `email`, `grade`, `job_title`, `initials`, `start_date`, `leaving_date`, "
        "`working_minutes_per_day`, `working_days` (`Mon-Fri`), `chargeable_target_percent`, "
        "`approver_code`, `status` and `login_status`. With `rate.read` also `cost_rate` and "
        "`charge_rate` (the standard rates today, pounds per hour). Text cells that start with "
        "`=`, `+`, `-` or `@` are prefixed with `'`. Requires `data.import_export` and "
        "`member.read`."
    ),
    responses=_CSV_RESPONSES,
    response_class=Response,
)
async def export_staff(org: StaffExporter, session: SessionDep) -> Response:
    return _csv(await StaffExportService(session).export_csv(org), "staff.csv")


@router.post(
    "/imports/staff",
    summary="Import staff from CSV",
    description=(
        _UPLOAD_COMMON
        + "Columns: `full_name`, `grade` (code or name) and `start_date` are required; "
        "optional `code` (default: the staff number sequence), `email`, `job_title`, "
        "`initials`, `leaving_date`, `working_minutes_per_day` (or `hours_per_day`), "
        "`working_days` (`Mon-Fri`, `Mon;Wed;Fri` or `1,2,3,4,5`), "
        "`chargeable_target_percent` (`75`), `approver_code` (an existing active staff member "
        "who can sign in) and, with `rate.manage` as well, `cost_rate`, `charge_rate` "
        "(pounds per hour) and `rate_effective_from` (default `start_date`), which create "
        "staff-level rates. Staff are created without a login, like `POST /staff`; existing "
        "staff are never changed (a used code or email is a row error). Writes one "
        "`staff.imported` audit entry. " + _UPLOAD_ERRORS + " Requires `data.import_export` "
        "and `member.invite`."
    ),
    responses=error_responses(400, 403, 404, 409, 413, 415),
    openapi_extra=_UPLOAD,
)
async def import_staff(
    request: Request,
    org: StaffImporter,
    session: SessionDep,
    ctx: ContextDep,
    dry_run: DryRun = False,
) -> ApiResponse[BulkImportResult]:
    result = await StaffImportService(session).run(
        org,
        request.headers.get("content-type", ""),
        await _read_body(request),
        dry_run=dry_run,
        rctx=ctx,
    )
    message = "Import checked" if dry_run else f"{result.created} staff member(s) imported"
    return ok(result, message)


# --- Rates -------------------------------------------------------------------------------


@router.get(
    "/rates/export",
    summary="Export rates as CSV",
    description=(
        "Every rate rule of every level as CSV (UTF-8 with BOM): `kind`, `staff_code`, "
        "`grade`, `client_code`, `service_code`, `rate` (pounds per hour), `effective_from`, "
        "`effective_to`, `level`, `status` (current, future or ended today) and the staff, "
        "client and service names. The first eight columns are what `POST /imports/rates` "
        "reads. Cost rules are included: `rate.read` is what shows cost rates. Text cells that "
        "start with `=`, `+`, `-` or `@` are prefixed with `'`. Requires `data.import_export` "
        "and `rate.read`."
    ),
    responses=_CSV_RESPONSES,
    response_class=Response,
)
async def export_rates(org: RateExporter, session: SessionDep) -> Response:
    return _csv(await RateExportService(session).export_csv(org), "rates.csv")


@router.post(
    "/imports/rates",
    summary="Import rates from CSV",
    description=(
        _UPLOAD_COMMON
        + "One row is one rate rule: `kind` (`cost` or `charge`), `rate` (pounds per hour) "
        "and `effective_from` are required; `effective_to` is optional (open-ended). Which of "
        "`staff_code`, `grade` (code or name), `client_code` and `service_code` are filled "
        "decides the level as for `POST /rates` (client + service + staff, client + service, "
        "client + staff, client, service + grade, staff or grade; cost rates only for staff "
        "or grade); other combinations are row errors. Rows are written like `POST /rates`, "
        "including the overlap rule (row error `RATE_OVERLAP`); `close_previous=true` ends "
        "an earlier open-ended rule of the same scope the day before instead. Writes one "
        "`rate.imported` audit entry and a history row per rule. "
        + _UPLOAD_ERRORS
        + " Requires `data.import_export` and `rate.manage`."
    ),
    responses=error_responses(400, 403, 404, 409, 413, 415),
    openapi_extra=_UPLOAD,
)
async def import_rates(
    request: Request,
    org: RateImporter,
    session: SessionDep,
    ctx: ContextDep,
    dry_run: DryRun = False,
    close_previous: Annotated[
        bool,
        Query(
            description=(
                "End-date the open-ended rule of the same scope the day before a row's "
                "`effective_from` instead of reporting `RATE_OVERLAP`"
            )
        ),
    ] = False,
) -> ApiResponse[BulkImportResult]:
    result = await RateImportService(session).run(
        org,
        request.headers.get("content-type", ""),
        await _read_body(request),
        dry_run=dry_run,
        close_previous=close_previous,
        rctx=ctx,
    )
    message = "Import checked" if dry_run else f"{result.created} rate(s) imported"
    return ok(result, message)


# --- Jobs --------------------------------------------------------------------------------


@router.get(
    "/jobs/export",
    summary="Export jobs as CSV",
    description=(
        "Every job that is not deleted, ordered by number, as CSV (UTF-8 with BOM): "
        "`number`, `name`, `client_code`, `service_code`, `status`, `billing_basis`, "
        "`fixed_fee`, `hourly_rate`, `budget_hours`, `budget_fee`, `budget_cost` (pounds), "
        "`period_start`, "
        "`period_end`, `start_date`, `due_date`, `manager` and `partner` (staff codes), "
        "`assignees` (staff codes separated by `;`), `description` and `client_name`. No "
        "actuals or WIP. Text cells that start with `=`, `+`, `-` or `@` are prefixed with "
        "`'`. Requires `data.import_export` and `job.read`."
    ),
    responses=_CSV_RESPONSES,
    response_class=Response,
)
async def export_jobs(org: JobExporter, session: SessionDep) -> Response:
    return _csv(await JobExportService(session).export_csv(org), "jobs.csv")


@router.post(
    "/imports/jobs",
    summary="Import jobs from CSV",
    description=(
        _UPLOAD_COMMON
        + "One row is one job, created like `POST /jobs`: `name` and `client_code` are "
        "required; optional `service_code`, `status` (`planned` or `in_progress`), "
        "`billing_basis` (`hourly`, `fixed` or `non_billable`; default from the service), "
        "`fixed_fee`, `hourly_rate`, `budget_hours`, `budget_fee`, `budget_cost`, `period_start`, "
        "`period_end`, `start_date`, `due_date`, `manager`, `partner` (a staff code or email "
        "address), `assignees` (staff codes or emails separated by `;`) and `description`. "
        "Numbers come from the job sequence (a `number` column is ignored). Writes a "
        "`job.created` entry per job and one `job.imported` entry. "
        + _UPLOAD_ERRORS
        + " Requires `data.import_export` and `job.create`."
    ),
    responses=error_responses(400, 403, 404, 409, 413, 415),
    openapi_extra=_UPLOAD,
)
async def import_jobs(
    request: Request,
    org: JobImporter,
    session: SessionDep,
    ctx: ContextDep,
    dry_run: DryRun = False,
) -> ApiResponse[BulkImportResult]:
    result = await JobImportService(session).run(
        org,
        request.headers.get("content-type", ""),
        await _read_body(request),
        dry_run=dry_run,
        rctx=ctx,
    )
    message = "Import checked" if dry_run else f"{result.created} job(s) imported"
    return ok(result, message)
