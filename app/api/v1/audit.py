"""Audit-log routes: the list, the history of one record and the export. HTTP only: the rules
live in ``AuditLogService``."""

import uuid
from dataclasses import replace
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, PageParamsDep, require
from app.schemas.audit import AuditFilters, AuditHistoryEntryRead, AuditLogEntryRead
from app.schemas.common import ApiResponse, error_responses, ok_page
from app.services.audit_logs import AuditLogService
from app.services.policy import OrgContext
from app.services.xlsx_export import XLSX_MEDIA_TYPE

router = APIRouter(prefix="/organizations/{organization_id}/audit-logs", tags=["Audit"])


def get_audit_log_service(session: SessionDep) -> AuditLogService:
    return AuditLogService(session)


AuditLogServiceDep = Annotated[AuditLogService, Depends(get_audit_log_service)]
Reader = Annotated[OrgContext, Depends(require("audit_log.read"))]
Exporter = Annotated[OrgContext, Depends(require("audit_log.read", "audit_log.export"))]


def audit_filters(
    action: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=100,
            description="Exact action, or a prefix ending in a dot such as `invoice.`",
        ),
    ] = None,
    actor_user_id: Annotated[
        uuid.UUID | None, Query(description="Only actions of this user")
    ] = None,
    created_from: Annotated[
        datetime | None, Query(alias="from", description="Earliest timestamp, inclusive")
    ] = None,
    created_to: Annotated[
        datetime | None, Query(alias="to", description="Latest timestamp, inclusive")
    ] = None,
    q: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=200,
            description=(
                "Search text: matches (case-insensitively) the action, the record type, the "
                "address, the person's name or email and the recorded details"
            ),
        ),
    ] = None,
) -> AuditFilters:
    return AuditFilters(
        action=action,
        actor_user_id=actor_user_id,
        created_from=created_from,
        created_to=created_to,
        q=q.strip() or None if q else None,
    )


FiltersDep = Annotated[AuditFilters, Depends(audit_filters)]


def record_filters(
    filters: FiltersDep,
    target_type: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=60,
            pattern=r"^[a-z][a-z0-9_]*$",
            description="Only entries about records of this type (`client`, `invoice` ...)",
        ),
    ] = None,
    target_id: Annotated[
        uuid.UUID | None, Query(description="Only entries about this record")
    ] = None,
) -> AuditFilters:
    return replace(filters, target_type=target_type, target_id=target_id)


ListFiltersDep = Annotated[AuditFilters, Depends(record_filters)]

_EXPORT_RESPONSES: dict[int | str, dict[str, Any]] = {
    **error_responses(403, 404),
    200: {
        "description": "The file; `X-Export-Row-Count` and `X-Export-Truncated` describe it",
        "content": {
            "text/csv": {"schema": {"type": "string"}},
            XLSX_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}},
        },
    },
}


@router.get(
    "",
    summary="List audit-log entries",
    description=(
        "Security-relevant actions recorded for this organization, newest first, "
        "cursor-paginated. Account-level events such as sign-ins belong to no organization "
        "and never appear. `action` matches exactly, or as a prefix when it ends with a dot "
        "(`member.`). `from` and `to` are inclusive ISO 8601 timestamps; one without an "
        "offset is read as UTC. `target_type` and `target_id` narrow the entries to one kind "
        "of record or one record, `q` searches them. Every entry carries its `label`, a "
        "`summary` and the normalised `changes` (field, before, after) next to the recorded "
        "`metadata`; cost figures are null without `rate.read`. Requires `audit_log.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_audit_logs(
    ctx: Reader,
    service: AuditLogServiceDep,
    page: PageParamsDep,
    filters: ListFiltersDep,
) -> ApiResponse[list[AuditLogEntryRead]]:
    result = await service.list(ctx, filters, page)
    return ok_page(result.items, result.next_cursor)


@router.get(
    "/history",
    summary="History of one record",
    description=(
        "The entries about one record, newest first, cursor-paginated: who did what and when, "
        "with the normalised `changes` (field, before, after). The same filters as the list "
        "(`action`, `actor_user_id`, `from`, `to`, `q`) apply. Needs `audit_log.read`, or the "
        "read permission of the record's own type (`client.read` for a client, `invoice.read` "
        "for an invoice, `job.read`, `payment.read`, `time.read_all`, `expense.read_all`, "
        "`rate.read`, `ticket.read`, `period_lock.read`, ...; the full mapping is in "
        "`docs/modules/audit.md`): without it 403, whatever the id. Without `audit_log.read` "
        "`ip_address` is null. `include_related` adds, for a client, the entries that name "
        "the client in their details (contacts, services, jobs, time, invoices, payments ...), "
        "limited to the record types the caller may read. A record of another practice has no "
        "entries here."
    ),
    responses=error_responses(403, 404),
)
async def record_history(
    ctx: OrgContextDep,
    service: AuditLogServiceDep,
    page: PageParamsDep,
    filters: FiltersDep,
    target_type: Annotated[
        str,
        Query(
            min_length=1,
            max_length=60,
            pattern=r"^[a-z][a-z0-9_]*$",
            description="The record's type as it appears in the entries: `client`, `invoice` ...",
        ),
    ],
    target_id: Annotated[uuid.UUID, Query(description="The record's id")],
    include_related: Annotated[
        bool, Query(description="Also entries that name the record (clients only)")
    ] = False,
) -> ApiResponse[list[AuditHistoryEntryRead]]:
    result = await service.history(
        ctx, target_type, target_id, filters, page, include_related=include_related
    )
    return ok_page(result.items, result.next_cursor)


@router.get(
    "/export",
    summary="Export the audit log",
    description=(
        "The entries that match the list filters (`action`, `actor_user_id`, `from`, `to`, "
        "`target_type`, `target_id`, `q`) as `format=csv` (UTF-8 with BOM, CRLF; text that "
        "starts with `=`, `+`, `-` or `@` is prefixed with `'`) or `format=xlsx`, newest "
        "first: time (UTC), user, email, action, event, summary, record type and id, changes, "
        "address, device, request id and the recorded details. At most 50,000 entries are "
        "exported (the newest); a larger result is cut and `X-Export-Truncated` is `true` "
        "(the workbook also says so). The export is itself audited (`audit_log.exported`). "
        "Requires `audit_log.read` and `audit_log.export`."
    ),
    response_class=Response,
    responses=_EXPORT_RESPONSES,
)
async def export_audit_logs(
    ctx: Exporter,
    service: AuditLogServiceDep,
    rctx: ContextDep,
    filters: ListFiltersDep,
    format: Annotated[Literal["csv", "xlsx"], Query(description="File format")] = "csv",
) -> Response:
    result = await service.export(ctx, rctx, filters, format)
    return Response(
        content=result.content,
        media_type=result.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            "X-Export-Row-Count": str(result.row_count),
            "X-Export-Truncated": "true" if result.truncated else "false",
            "Cache-Control": "no-store",
        },
    )
