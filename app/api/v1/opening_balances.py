"""Opening-balance import: historic unbilled time from a client's previous system. HTTP only."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import require
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.opening_balances import OpeningBalanceTimeImportResult
from app.services.client_import import MAX_UPLOAD_BODY_BYTES, ImportTooLargeError
from app.services.opening_balance_import import OpeningBalanceImportService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Opening Balances"])

OpeningBalanceImporter = Annotated[OrgContext, Depends(require("opening_balance.import"))]


def get_opening_balance_service(session: SessionDep) -> OpeningBalanceImportService:
    return OpeningBalanceImportService(session)


OpeningBalanceServiceDep = Annotated[
    OpeningBalanceImportService, Depends(get_opening_balance_service)
]


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


@router.post(
    "/imports/opening_balance_time",
    summary="Import historic unbilled time from CSV",
    description=(
        "Upload a CSV (multipart/form-data field `file`, or a `text/csv` body) of historic "
        "unbilled time from a client's previous system: columns `client` (client code), "
        "`staff` (email), `date` (ISO `YYYY-MM-DD`), `hours` (decimal), and optionally "
        "`activity` (activity code), `job` (job number) and `billable` (Y/N, default Y). "
        "Resolved and validated like an ordinary time entry, then created the same way "
        "(`POST /time-entries/bulk`), so rates, timesheets and period locks behave "
        "identically; every row created is tagged so it can be shown apart from newly "
        "recorded time, and a weekly timesheet the import created outright is approved "
        "automatically so the imported time is immediately billable. At most 100 rows per "
        "file (the bulk time-entry create's own limit) and 5 MB. `dry_run=true` validates "
        "and returns per-row errors without writing. A real run is all-or-nothing: 422 "
        "`IMPORT_INVALID` (errors in `details`) or every row is created. 413 "
        "`FILE_TOO_LARGE`, 422 `IMPORT_TOO_MANY_ROWS` / `IMPORT_MISSING_COLUMNS`, 400 "
        "`FILE_INVALID`. Requires `opening_balance.import`."
    ),
    responses=error_responses(400, 403, 404, 409, 413, 415),
    openapi_extra={
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
    },
)
async def import_opening_balance_time(
    request: Request,
    org: OpeningBalanceImporter,
    service: OpeningBalanceServiceDep,
    ctx: ContextDep,
    dry_run: Annotated[bool, Query(description="Validate only; write nothing")] = False,
) -> ApiResponse[OpeningBalanceTimeImportResult]:
    body = await _read_body(request)
    result = await service.run(
        org, request.headers.get("content-type", ""), body, dry_run=dry_run, rctx=ctx
    )
    count = result.created
    message = (
        "Import checked"
        if dry_run
        else f"{count} time {'entry' if count == 1 else 'entries'} imported"
    )
    return ok(result, message)
