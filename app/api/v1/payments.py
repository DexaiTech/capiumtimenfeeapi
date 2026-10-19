"""Payments, allocations and the client statement. HTTP only."""

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.v1.invoices import PDF_RESPONSE, pdf_response
from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.repositories.payments import PaymentFilters
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.payments import (
    AllocationsCreate,
    PaymentCreate,
    PaymentMethod,
    PaymentRead,
    PaymentReverse,
    PaymentState,
    PaymentVoid,
    StatementRead,
)
from app.services.payments import PaymentService
from app.services.policy import OrgContext
from app.services.statements import StatementService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Payments"])


def get_payment_service(session: SessionDep) -> PaymentService:
    return PaymentService(session)


def get_statement_service(session: SessionDep) -> StatementService:
    return StatementService(session)


PaymentServiceDep = Annotated[PaymentService, Depends(get_payment_service)]
StatementServiceDep = Annotated[StatementService, Depends(get_statement_service)]
Reader = Annotated[OrgContext, Depends(require("payment.read"))]
Recorder = Annotated[OrgContext, Depends(require("payment.record"))]
StatementReader = Annotated[OrgContext, Depends(require("invoice.read", "payment.read"))]


@router.get(
    "/payments",
    summary="List payments",
    description=(
        "Payments newest first by date received, cursor-paginated. Filters: client, date "
        "range, method (repeatable), state (`unallocated`, `part_allocated`, `allocated`, "
        "`void`, `reversed`, or `on_account` = some money unallocated), `q` (reference or "
        "notes). Requires `payment.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_payments(
    org: Reader,
    service: PaymentServiceDep,
    page: PageParamsDep,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client's")] = None,
    date_from: Annotated[date | None, Query(description="Received on or after")] = None,
    date_to: Annotated[date | None, Query(description="Received on or before")] = None,
    method: Annotated[list[PaymentMethod] | None, Query(description="Method")] = None,
    state: Annotated[
        PaymentState | Literal["on_account"] | None,
        Query(description="Allocation state; on_account = some money unallocated"),
    ] = None,
    q: Annotated[str | None, Query(max_length=100, description="Reference or notes")] = None,
) -> ApiResponse[list[PaymentRead]]:
    filters = PaymentFilters(
        client_id=client_id,
        date_from=date_from,
        date_to=date_to,
        methods=method,
        state=state,
        q=q.strip() if q else None,
    )
    result, items = await service.list_payments(org, filters, page)
    return ok_page(items, result.next_cursor)


@router.post(
    "/payments",
    status_code=status.HTTP_201_CREATED,
    summary="Record a payment",
    description=(
        "Money received from a client, optionally allocated at once: either `allocations` "
        "(each to one of the client's issued invoices, at most its balance: 409 "
        "`ALLOCATION_EXCEEDS_BALANCE`; together at most the amount: 409 "
        "`ALLOCATION_EXCEEDS_PAYMENT`; another client's invoice: 422 `CLIENT_MISMATCH`; a "
        "draft or void invoice: 409 `INVOICE_NOT_ISSUED`) or `auto_allocate` (oldest open "
        "invoices first). The rest stays on account. Requires `payment.record`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_payment(
    body: PaymentCreate, org: Recorder, service: PaymentServiceDep, ctx: ContextDep
) -> ApiResponse[PaymentRead]:
    return ok(await service.create(org, body, ctx), "Payment recorded")


@router.get(
    "/payments/{payment_id}",
    summary="Get a payment",
    description="One payment with its allocations. Requires `payment.read`.",
    responses=error_responses(403, 404),
)
async def get_payment(
    payment_id: uuid.UUID, org: Reader, service: PaymentServiceDep
) -> ApiResponse[PaymentRead]:
    return ok(await service.get(org, payment_id))


@router.post(
    "/payments/{payment_id}/allocations",
    summary="Allocate a payment",
    description=(
        "Allocates unallocated money of the payment to invoices, by `allocations` or "
        "`auto_allocate`, with the same checks as recording. An invoice already allocated "
        "from this payment gets its allocation increased. 409 `NOTHING_TO_ALLOCATE` when "
        "auto-allocation finds nothing to do, `PAYMENT_VOID` for a void payment. Requires "
        "`payment.record`."
    ),
    responses=error_responses(403, 404, 409),
)
async def allocate_payment(
    payment_id: uuid.UUID,
    body: AllocationsCreate,
    org: Recorder,
    service: PaymentServiceDep,
    ctx: ContextDep,
) -> ApiResponse[PaymentRead]:
    return ok(await service.allocate(org, payment_id, body, ctx), "Payment allocated")


@router.delete(
    "/payments/{payment_id}/allocations/{allocation_id}",
    summary="Remove an allocation",
    description=(
        "Unallocates: the invoice's balance goes back up and the money is on account again. "
        "Requires `payment.record`."
    ),
    responses=error_responses(403, 404, 409),
)
async def remove_allocation(
    payment_id: uuid.UUID,
    allocation_id: uuid.UUID,
    org: Recorder,
    service: PaymentServiceDep,
    ctx: ContextDep,
) -> ApiResponse[PaymentRead]:
    payment = await service.remove_allocation(org, payment_id, allocation_id, ctx)
    return ok(payment, "Allocation removed")


@router.post(
    "/payments/{payment_id}/void",
    summary="Void a payment",
    description=(
        "Voids a payment that is fully unallocated (409 `PAYMENT_ALLOCATED` otherwise, "
        "`PAYMENT_VOID` when already void). Requires `payment.record`."
    ),
    responses=error_responses(403, 404, 409),
)
async def void_payment(
    payment_id: uuid.UUID,
    org: Recorder,
    service: PaymentServiceDep,
    ctx: ContextDep,
    body: PaymentVoid | None = None,
) -> ApiResponse[PaymentRead]:
    reason = body.reason if body else None
    return ok(await service.void(org, payment_id, reason, ctx), "Payment voided")


@router.post(
    "/payments/{payment_id}/reverse",
    summary="Reverse a payment",
    description=(
        "Reverses a payment in one step, for example a bounced cheque or a returned direct "
        "debit: every allocation is removed (each invoice it paid owes that money again and "
        "its status is recalculated), and the payment is marked reversed with the required "
        "`reason` and the `bounced` flag, who and when. The practice's payment staff are "
        "notified and the reversal is audited. 409 `PAYMENT_REVERSED` when already reversed, "
        "`PAYMENT_VOID` when void, `PAYMENT_CHANGED` when it was allocated at the same "
        "moment (try again); 422 without a reason. Requires `payment.record`."
    ),
    responses=error_responses(403, 404, 409),
)
async def reverse_payment(
    payment_id: uuid.UUID,
    body: PaymentReverse,
    org: Recorder,
    service: PaymentServiceDep,
    ctx: ContextDep,
) -> ApiResponse[PaymentRead]:
    return ok(await service.reverse(org, payment_id, body, ctx), "Payment reversed")


@router.get(
    "/clients/{client_id}/statement",
    summary="Client statement",
    description=(
        "Invoices (+), credit notes (-) and payments (-) in date order with a running "
        "balance: the opening balance before `date_from` (0 without it), the entries up to "
        "`date_to` (default today), the closing balance and the cash on account. Void "
        "invoices and void payments are left out; a reversed payment shows, followed by its "
        "reversal (+) on the day it was reversed. Requires `invoice.read` and `payment.read`."
    ),
    responses=error_responses(403, 404),
)
async def client_statement(
    client_id: uuid.UUID,
    org: StatementReader,
    service: StatementServiceDep,
    date_from: Annotated[date | None, Query(description="Start (inclusive)")] = None,
    date_to: Annotated[date | None, Query(description="End (inclusive); default today")] = None,
) -> ApiResponse[StatementRead]:
    return ok(await service.statement(org, client_id, date_from, date_to))


@router.get(
    "/clients/{client_id}/statement/pdf",
    summary="Client statement PDF",
    description="The statement as a PDF. Requires `invoice.read` and `payment.read`.",
    response_class=Response,
    responses={**error_responses(403, 404), **PDF_RESPONSE},
)
async def client_statement_pdf(
    client_id: uuid.UUID,
    org: StatementReader,
    service: StatementServiceDep,
    date_from: Annotated[date | None, Query(description="Start (inclusive)")] = None,
    date_to: Annotated[date | None, Query(description="End (inclusive); default today")] = None,
) -> Response:
    filename, content = await service.pdf(org, client_id, date_from, date_to)
    return pdf_response(filename, content)
