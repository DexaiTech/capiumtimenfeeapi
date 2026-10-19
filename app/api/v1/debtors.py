"""Debtors and payment reminders. HTTP only: every rule lives in the services."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.dependencies.common import ContextDep, SessionDep, SettingsDep
from app.dependencies.organization import OrgContextDep, PageParamsDep, require
from app.repositories.payment_reminders import ReminderFilters
from app.schemas.common import ApiResponse, PageMeta, error_responses, ok, ok_page
from app.schemas.debtors import (
    DebtorClientInvoicesRead,
    DebtorSort,
    DebtorsRead,
    ReminderRead,
    ReminderRequest,
    ReminderResult,
    ReminderSource,
    ReminderStatus,
    SortOrder,
)
from app.services.debtors import DebtorQuery, DebtorService
from app.services.payment_reminders import PaymentReminderService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Debtors"])


def get_debtor_service(session: SessionDep) -> DebtorService:
    return DebtorService(session)


def get_reminder_service(session: SessionDep, settings: SettingsDep) -> PaymentReminderService:
    return PaymentReminderService(session, settings)


DebtorServiceDep = Annotated[DebtorService, Depends(get_debtor_service)]
ReminderServiceDep = Annotated[PaymentReminderService, Depends(get_reminder_service)]
Recorder = Annotated[OrgContext, Depends(require("payment.record"))]

_AS_AT = "The date the balances are as at (default today); a future date is 422"


@router.get(
    "/debtors",
    summary="Aged debtors",
    description=(
        "The clients that owe money on issued invoices, aged by days past the due date as at a "
        "date: `current` (not past due), `days_1_30`, `days_31_60`, `days_61_90` and "
        "`days_90_plus` (91 days or more), with the total, the overdue part, the oldest due date "
        "and the latest reminder. The figures are the Aged Debtors report's. `totals` cover "
        "every matching client, not only the page. Filters: client, the client's partner or "
        "manager, `min_balance` (clients owing at least this much, pence). Sorted by `sort` "
        "(`client` A-Z by default, else largest first unless `order=asc`), cursor-paginated; a "
        "cursor made for another sort or filter is 422. Needs any of `payment.read`, "
        "`invoice.read` or `report.financial`."
    ),
    responses=error_responses(403),
)
async def list_debtors(
    org: OrgContextDep,
    service: DebtorServiceDep,
    page: PageParamsDep,
    as_at: Annotated[date | None, Query(description=_AS_AT)] = None,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client")] = None,
    partner_user_id: Annotated[
        uuid.UUID | None, Query(description="Clients whose partner is this person")
    ] = None,
    manager_user_id: Annotated[
        uuid.UUID | None, Query(description="Clients whose manager is this person")
    ] = None,
    min_balance: Annotated[
        int | None, Query(ge=0, le=10**12, description="Only clients owing at least this (pence)")
    ] = None,
    sort: Annotated[DebtorSort, Query(description="Sort column")] = "client",
    order: Annotated[
        SortOrder | None, Query(description="Default `asc` for `client`, else `desc`")
    ] = None,
) -> ApiResponse[DebtorsRead]:
    query = DebtorQuery(
        as_at=as_at,
        client_id=client_id,
        partner_user_id=partner_user_id,
        manager_user_id=manager_user_id,
        min_balance=min_balance,
        sort=sort,
        order=order,
    )
    data, next_cursor = await service.list_debtors(org, query, page)
    return ApiResponse[DebtorsRead](
        data=data, meta=PageMeta(next_cursor=next_cursor, has_more=next_cursor is not None)
    )


@router.get(
    "/debtors/reminders",
    summary="Payment reminder history",
    description=(
        "Every reminder about an overdue invoice, newest first, cursor-paginated: the client, "
        "the invoice, the template, whether a person (`manual`) or the daily job (`scheduled`) "
        "sent it, its status (`queued`, `sent`, `failed`, `skipped`), the address(es), the days "
        "overdue and amount due at the time and the error of a failed one. Filters: client, "
        "invoice, status, source and the day it was sent (`date_from`, `date_to`). Needs any of "
        "`payment.read`, `invoice.read` or `report.financial`."
    ),
    responses=error_responses(403),
)
async def list_reminders(
    org: OrgContextDep,
    service: ReminderServiceDep,
    page: PageParamsDep,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client's")] = None,
    invoice_id: Annotated[uuid.UUID | None, Query(description="Only this invoice's")] = None,
    status_: Annotated[ReminderStatus | None, Query(alias="status", description="Status")] = None,
    source: Annotated[ReminderSource | None, Query(description="Who sent it")] = None,
    date_from: Annotated[date | None, Query(description="Sent on or after")] = None,
    date_to: Annotated[date | None, Query(description="Sent on or before")] = None,
) -> ApiResponse[list[ReminderRead]]:
    filters = ReminderFilters(
        client_id=client_id,
        invoice_id=invoice_id,
        status=status_,
        source=source,
        date_from=date_from,
        date_to=date_to,
    )
    items, next_cursor = await service.history(org, filters, page)
    return ok_page(items, next_cursor)


@router.post(
    "/debtors/reminders",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Send payment reminders",
    description=(
        "Reminds clients about their overdue invoices: give exactly one of `client_ids` (every "
        "overdue invoice of each client) or `invoice_ids`. One email per client lists its "
        "invoices; it goes to the client's billing recipients (invoice email, else billing "
        "contacts, else the primary contact, else the client email) and is queued as a job (202). "
        "An invoice is reminded at most once a day (practice time zone). `template` is "
        "`client_payment_reminder` (default) or `client_payment_final_notice`. Invoices that "
        "cannot be reminded about come back in `skipped` with the reason "
        "(`already_reminded_today`, `not_overdue`, `no_balance`, `not_issued`, "
        "`no_recipient`). With `dry_run` nothing is "
        "written or queued and the answer is 200, with the emails that would go out. An unknown "
        "id, or another practice's, is 404. Requires `payment.record`."
    ),
    responses=error_responses(403, 404),
)
async def send_reminders(
    body: ReminderRequest,
    org: Recorder,
    service: ReminderServiceDep,
    ctx: ContextDep,
    response: Response,
) -> ApiResponse[ReminderResult]:
    result = await service.send(org, body, ctx)
    if result.dry_run:
        response.status_code = status.HTTP_200_OK
        return ok(result, "Nothing was sent: this is what would go out")
    return ok(result, "Reminders queued")


@router.get(
    "/debtors/{client_id}/invoices",
    summary="A debtor's open invoices",
    description=(
        "The client's invoices that still owed money as at the date, oldest due date first (at "
        "most 500), each with its days overdue, bucket, amounts, display status and reminders, "
        "and the client's bucket totals. `source` is `opening` for opening-balance invoices, "
        "otherwise null. 404 for an unknown client or another practice's. Needs any of "
        "`payment.read`, `invoice.read` or `report.financial`."
    ),
    responses=error_responses(403, 404),
)
async def debtor_invoices(
    client_id: uuid.UUID,
    org: OrgContextDep,
    service: DebtorServiceDep,
    as_at: Annotated[date | None, Query(description=_AS_AT)] = None,
) -> ApiResponse[DebtorClientInvoicesRead]:
    return ok(await service.client_invoices(org, client_id, as_at))
