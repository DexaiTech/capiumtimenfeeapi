"""Weekly timesheets: submission and approval. HTTP only: rules live in TimesheetService."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, PageParamsDep, require
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.time import (
    BulkApprove,
    TimesheetApprove,
    TimesheetDetail,
    TimesheetRead,
    TimesheetReject,
    TimesheetStatus,
)
from app.services.policy import OrgContext
from app.services.timesheets import TimesheetService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Timesheets"])


def get_timesheet_service(session: SessionDep) -> TimesheetService:
    return TimesheetService(session)


TimesheetServiceDep = Annotated[TimesheetService, Depends(get_timesheet_service)]
ApproverDep = Annotated[OrgContext, Depends(require("timesheet.approve"))]

_SELF = (
    " Nobody decides on their own timesheet unless they hold `organization.update` "
    "(403 `SELF_APPROVAL_FORBIDDEN`)."
)


@router.get(
    "/timesheets",
    summary="List timesheets",
    description=(
        "Timesheets newest week first, cursor-paginated, with totals. `user_id`: `me` "
        "(default), a user id (needs `time.read_all` or being their manager) or `all` "
        "(everyone with `time.read_all`, else the caller and the people they manage). "
        "`from`/`to` bound `week_start` (inclusive); `status` repeats."
    ),
    responses=error_responses(403, 404),
)
async def list_timesheets(
    org: OrgContextDep,
    service: TimesheetServiceDep,
    page: PageParamsDep,
    user_id: Annotated[str | None, Query(description="'me' (default), a user id or 'all'")] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    sheet_status: Annotated[list[TimesheetStatus] | None, Query(alias="status")] = None,
) -> ApiResponse[list[TimesheetRead]]:
    result = await service.list_sheets(
        org,
        user_id=user_id,
        date_from=date_from,
        date_to=date_to,
        statuses=sheet_status,
        params=page,
    )
    return ok_page(result.items, result.next_cursor)


@router.get(
    "/timesheets/{timesheet_id}",
    summary="Get a timesheet",
    description=(
        "One timesheet with its entries, totals and minutes per day. Own, or another "
        "person's with `time.read_all` or as their manager."
    ),
    responses=error_responses(403, 404),
)
async def get_timesheet(
    timesheet_id: uuid.UUID, org: OrgContextDep, service: TimesheetServiceDep
) -> ApiResponse[TimesheetDetail]:
    return ok(await service.get(org, timesheet_id))


@router.post(
    "/timesheets/{timesheet_id}/submit",
    summary="Submit a timesheet",
    description=(
        "The owner submits an `open` or `rejected` sheet for approval; its entries lock. "
        "Needs at least one entry (422 `TIMESHEET_EMPTY`). Another status answers 409 "
        "`INVALID_STATUS_TRANSITION`."
    ),
    responses=error_responses(403, 404, 409),
)
async def submit_timesheet(
    timesheet_id: uuid.UUID, org: OrgContextDep, service: TimesheetServiceDep, ctx: ContextDep
) -> ApiResponse[TimesheetDetail]:
    return ok(await service.submit(org, timesheet_id, ctx), "Timesheet submitted")


@router.post(
    "/timesheets/{timesheet_id}/recall",
    summary="Recall a timesheet",
    description="The owner pulls a `submitted` (not yet decided) sheet back to `open`.",
    responses=error_responses(403, 404, 409),
)
async def recall_timesheet(
    timesheet_id: uuid.UUID, org: OrgContextDep, service: TimesheetServiceDep, ctx: ContextDep
) -> ApiResponse[TimesheetDetail]:
    return ok(await service.recall(org, timesheet_id, ctx), "Timesheet recalled")


@router.post(
    "/timesheets/{timesheet_id}/approve",
    summary="Approve a timesheet",
    description=(
        "Approves a `submitted` sheet, with an optional note. Requires `timesheet.approve`." + _SELF
    ),
    responses=error_responses(403, 404, 409),
)
async def approve_timesheet(
    timesheet_id: uuid.UUID,
    org: ApproverDep,
    service: TimesheetServiceDep,
    ctx: ContextDep,
    body: Annotated[TimesheetApprove | None, Body()] = None,
) -> ApiResponse[TimesheetDetail]:
    note = body.decision_note if body else None
    return ok(await service.approve(org, timesheet_id, note, ctx), "Timesheet approved")


@router.post(
    "/timesheets/{timesheet_id}/reject",
    summary="Reject a timesheet",
    description=(
        "Sends a `submitted` sheet back to its owner with a `decision_note` (required); "
        "they edit and resubmit it. Requires `timesheet.approve`." + _SELF
    ),
    responses=error_responses(403, 404, 409),
)
async def reject_timesheet(
    timesheet_id: uuid.UUID,
    body: TimesheetReject,
    org: ApproverDep,
    service: TimesheetServiceDep,
    ctx: ContextDep,
) -> ApiResponse[TimesheetDetail]:
    result = await service.reject(org, timesheet_id, body.decision_note, ctx)
    return ok(result, "Timesheet rejected")


@router.post(
    "/timesheets/{timesheet_id}/reopen",
    summary="Reopen an approved timesheet",
    description=(
        "Moves an `approved` sheet back to `open` while none of its time is billed, in a "
        "draft or written off (409 `TIMESHEET_HAS_BILLED_TIME`). Requires "
        "`timesheet.approve`." + _SELF
    ),
    responses=error_responses(403, 404, 409),
)
async def reopen_timesheet(
    timesheet_id: uuid.UUID, org: ApproverDep, service: TimesheetServiceDep, ctx: ContextDep
) -> ApiResponse[TimesheetDetail]:
    return ok(await service.reopen(org, timesheet_id, ctx), "Timesheet reopened")


@router.delete(
    "/timesheets/{timesheet_id}",
    summary="Delete a timesheet",
    description=(
        "Deletes an `open` or `rejected` sheet together with its time entries, all or "
        "nothing. Own sheets need `time.log`, others' `time.manage_all`. A submitted or "
        "approved sheet answers 409 `INVALID_STATUS_TRANSITION` (recall or reopen it "
        "first); time that is billed, in a draft or written off 409 "
        "`TIMESHEET_HAS_BILLED_TIME`; a locked period 409 `PERIOD_LOCKED`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_timesheet(
    timesheet_id: uuid.UUID, org: OrgContextDep, service: TimesheetServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.delete(org, timesheet_id, ctx)
    return ok(message="Timesheet deleted")


@router.get(
    "/approvals",
    summary="Approvals queue",
    description=(
        "Submitted timesheets waiting for a decision, oldest week first: of the people the "
        "caller manages, or everyone's with `time.read_all`."
    ),
    responses=error_responses(403, 404),
)
async def approvals_queue(
    org: OrgContextDep, service: TimesheetServiceDep, page: PageParamsDep
) -> ApiResponse[list[TimesheetRead]]:
    result = await service.approvals(org, page)
    return ok_page(result.items, result.next_cursor)


@router.post(
    "/approvals/bulk-approve",
    summary="Approve many timesheets",
    description=(
        "Approves up to 100 submitted sheets atomically: an unknown sheet (404), one that is "
        "not submitted (409) or one's own (403) rolls back all of them. Requires "
        "`timesheet.approve`." + _SELF
    ),
    responses=error_responses(403, 404, 409),
)
async def bulk_approve(
    body: BulkApprove, org: ApproverDep, service: TimesheetServiceDep, ctx: ContextDep
) -> ApiResponse[list[TimesheetRead]]:
    sheets = await service.bulk_approve(org, body.timesheet_ids, ctx)
    return ok(sheets, f"{len(sheets)} timesheets approved")
