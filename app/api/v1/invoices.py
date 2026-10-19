"""Invoices: drafts from WIP, lines, issue, void, PDF and email. HTTP only."""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.repositories.invoices import InvoiceFilters
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.invoices import (
    DisplayStatus,
    InvoiceAddWip,
    InvoiceApprove,
    InvoiceCreate,
    InvoiceIssue,
    InvoiceListSummary,
    InvoiceRead,
    InvoiceReject,
    InvoiceSend,
    InvoiceSent,
    InvoiceSubmit,
    InvoiceSummaryRead,
    InvoiceUpdate,
    InvoiceVoid,
    LineCreate,
    LineUpdate,
)
from app.services.invoices import InvoiceService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Invoices"])


def get_invoice_service(session: SessionDep) -> InvoiceService:
    return InvoiceService(session)


InvoiceServiceDep = Annotated[InvoiceService, Depends(get_invoice_service)]
Reader = Annotated[OrgContext, Depends(require("invoice.read"))]
Drafter = Annotated[OrgContext, Depends(require("invoice.create"))]
Issuer = Annotated[OrgContext, Depends(require("invoice.issue"))]
Approver = Annotated[OrgContext, Depends(require("invoice.approve"))]

PDF_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {"content": {"application/pdf": {}}, "description": "The PDF"}
}


def pdf_response(filename: str, content: bytes) -> Response:
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get(
    "/invoices",
    summary="List invoices",
    description=(
        "Invoices newest first, cursor-paginated. `status` filters by the derived display "
        "status (draft, awaiting_approval, approved, issued, part_paid, paid, credited, "
        "overdue, void; repeatable); `outstanding` keeps issued invoices with (true) or "
        "without (false) a balance; `date_from`/`date_to` filter the issue date; `q` matches "
        "the number or client name or code. Requires `invoice.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_invoices(
    org: Reader,
    service: InvoiceServiceDep,
    page: PageParamsDep,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client's")] = None,
    status_: Annotated[
        list[DisplayStatus] | None,
        Query(alias="status", description="Display status; repeat for several"),
    ] = None,
    date_from: Annotated[date | None, Query(description="Issued on or after")] = None,
    date_to: Annotated[date | None, Query(description="Issued on or before")] = None,
    q: Annotated[str | None, Query(max_length=100, description="Number or client")] = None,
    outstanding: Annotated[bool | None, Query(description="Issued with a balance")] = None,
) -> ApiResponse[list[InvoiceSummaryRead]]:
    filters = InvoiceFilters(
        client_id=client_id,
        statuses=status_,
        date_from=date_from,
        date_to=date_to,
        q=q.strip() if q else None,
        outstanding=outstanding,
    )
    items, next_cursor = await service.list_invoices(org, filters, page)
    return ok_page(items, next_cursor)


@router.get(
    "/invoices/summary",
    summary="Invoice counts per status",
    description=(
        "The numbers behind the status tabs of the invoice list: how many invoices are in "
        "each display status (each counted once), the total, what is outstanding and overdue "
        "on issued invoices and the gross total awaiting approval. Takes the list's "
        "`client_id`, `date_from`, `date_to` and `q` filters but no `status`. "
        "Requires `invoice.read`."
    ),
    responses=error_responses(403, 404),
)
async def invoice_summary(
    org: Reader,
    service: InvoiceServiceDep,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client's")] = None,
    date_from: Annotated[date | None, Query(description="Issued on or after")] = None,
    date_to: Annotated[date | None, Query(description="Issued on or before")] = None,
    q: Annotated[str | None, Query(max_length=100, description="Number or client")] = None,
) -> ApiResponse[InvoiceListSummary]:
    filters = InvoiceFilters(
        client_id=client_id, date_from=date_from, date_to=date_to, q=q.strip() if q else None
    )
    return ok(await service.list_summary(org, filters))


@router.post(
    "/invoices",
    status_code=status.HTTP_201_CREATED,
    summary="Draft an invoice from WIP",
    description=(
        "Creates a draft for the client billing the chosen WIP items (time entries and "
        "disbursements: all the client's and unbilled, else 422 `CLIENT_MISMATCH` / 409 "
        "`WIP_NOT_AVAILABLE`). Time becomes one line per job (`group_by=job`), per entry "
        "(`entry`) or one line (`single`); time on fixed-fee jobs always becomes one "
        "`fixed_fee` line per job at the fee; each disbursement its own line. `overrides` "
        "change a line's bill amount, description or VAT rate; the bill amount is "
        "apportioned over the line's items by WIP value. Items move to `in_draft`. "
        "Requires `invoice.create`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_invoice(
    body: InvoiceCreate, org: Drafter, service: InvoiceServiceDep, ctx: ContextDep
) -> ApiResponse[InvoiceRead]:
    return ok(await service.create_draft(org, body, ctx), "Draft invoice created")


@router.get(
    "/invoices/{invoice_id}",
    summary="Get an invoice",
    description=(
        "The invoice with its lines (each with the WIP it bills: WIP value, billed value "
        "and write-up/off), VAT summary, payments, credit notes, balance and display "
        "status. Requires `invoice.read`."
    ),
    responses=error_responses(403, 404),
)
async def get_invoice(
    invoice_id: uuid.UUID, org: Reader, service: InvoiceServiceDep
) -> ApiResponse[InvoiceRead]:
    return ok(await service.get(org, invoice_id))


@router.patch(
    "/invoices/{invoice_id}",
    summary="Update an invoice header",
    description=(
        "Changes a draft's issue date, due date, reference and notes. An invoice that is "
        "awaiting approval, approved, issued or void cannot be edited at all: 409 "
        "`INVOICE_NOT_EDITABLE`, whatever the body (reject a waiting invoice back to a "
        "draft to edit it). Requires `invoice.create`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_invoice(
    invoice_id: uuid.UUID,
    body: InvoiceUpdate,
    org: Drafter,
    service: InvoiceServiceDep,
    ctx: ContextDep,
) -> ApiResponse[InvoiceRead]:
    return ok(await service.update(org, invoice_id, body, ctx), "Invoice updated")


@router.delete(
    "/invoices/{invoice_id}",
    summary="Delete a draft",
    description=(
        "Deletes a draft invoice (409 `INVOICE_NOT_DRAFT` otherwise) and returns its WIP "
        "items to `unbilled`. Requires `invoice.create`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_invoice(
    invoice_id: uuid.UUID, org: Drafter, service: InvoiceServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.delete_draft(org, invoice_id, ctx)
    return ok(message="Draft deleted")


@router.post(
    "/invoices/{invoice_id}/lines",
    status_code=status.HTTP_201_CREATED,
    summary="Add a manual line",
    description=(
        "A line that bills no WIP (`kind=manual`): net = round(quantity x unit amount) "
        "unless `net_amount` is given; negative for a discount. VAT: the line's rate, else "
        "the service's, else the client's default, else the practice default. The invoice "
        "total may not go below zero (422 `INVOICE_TOTAL_NEGATIVE`). Drafts only. "
        "Requires `invoice.create`."
    ),
    responses=error_responses(403, 404, 409),
)
async def add_line(
    invoice_id: uuid.UUID,
    body: LineCreate,
    org: Drafter,
    service: InvoiceServiceDep,
    ctx: ContextDep,
) -> ApiResponse[InvoiceRead]:
    return ok(await service.add_line(org, invoice_id, body, ctx), "Line added")


@router.patch(
    "/invoices/{invoice_id}/lines/{line_id}",
    summary="Update a line",
    description=(
        "Changes a draft line. A new `net_amount` on a line that bills WIP is re-apportioned "
        "over its items (it cannot be negative: 422 `NEGATIVE_WIP_LINE`); changing quantity "
        "or unit amount alone recomputes the net. `vat_rate_id: null` re-applies the default "
        "rate. Requires `invoice.create`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_line(
    invoice_id: uuid.UUID,
    line_id: uuid.UUID,
    body: LineUpdate,
    org: Drafter,
    service: InvoiceServiceDep,
    ctx: ContextDep,
) -> ApiResponse[InvoiceRead]:
    return ok(await service.update_line(org, invoice_id, line_id, body, ctx), "Line updated")


@router.delete(
    "/invoices/{invoice_id}/lines/{line_id}",
    summary="Remove a line",
    description=(
        "Removes a draft line; the WIP it billed returns to `unbilled`. Requires `invoice.create`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_line(
    invoice_id: uuid.UUID,
    line_id: uuid.UUID,
    org: Drafter,
    service: InvoiceServiceDep,
    ctx: ContextDep,
) -> ApiResponse[InvoiceRead]:
    return ok(await service.delete_line(org, invoice_id, line_id, ctx), "Line removed")


@router.post(
    "/invoices/{invoice_id}/wip",
    summary="Add WIP to a draft",
    description=(
        "Adds more of the client's WIP items to a draft as new lines (time on a fixed-fee "
        "job already on the draft joins that job's fixed-fee line). Same rules as drafting. "
        "Requires `invoice.create`."
    ),
    responses=error_responses(403, 404, 409),
)
async def add_wip(
    invoice_id: uuid.UUID,
    body: InvoiceAddWip,
    org: Drafter,
    service: InvoiceServiceDep,
    ctx: ContextDep,
) -> ApiResponse[InvoiceRead]:
    return ok(await service.add_wip(org, invoice_id, body, ctx), "WIP added")


@router.post(
    "/invoices/{invoice_id}/submit",
    summary="Submit an invoice for approval",
    description=(
        "Sends a draft with lines to the approvers (members holding `invoice.approve`, "
        "notified in the app and by email): its status becomes `awaiting_approval` and it "
        "is locked. Only when approval is required: the practice setting is on and the "
        "gross total reaches `invoice_approval_threshold` (409 `APPROVAL_NOT_REQUIRED` "
        "otherwise: issue it directly). 409 `INVOICE_NOT_DRAFT` for any other status, "
        "`INVOICE_EMPTY` without lines. The optional `note` goes to the approval history. "
        "Requires `invoice.create`."
    ),
    responses=error_responses(403, 404, 409),
)
async def submit_invoice(
    invoice_id: uuid.UUID,
    org: Drafter,
    service: InvoiceServiceDep,
    ctx: ContextDep,
    body: InvoiceSubmit | None = None,
) -> ApiResponse[InvoiceRead]:
    submitted = await service.submit(org, invoice_id, body or InvoiceSubmit(), ctx)
    return ok(submitted, "Invoice submitted for approval")


@router.post(
    "/invoices/{invoice_id}/approve",
    summary="Approve an invoice",
    description=(
        "Approves an invoice that is `awaiting_approval` (409 `INVOICE_NOT_AWAITING_APPROVAL` "
        "otherwise): its status becomes `approved` and it can be issued by someone holding "
        "`invoice.issue`. Whoever created or submitted the invoice cannot approve it (409 "
        "`SELF_APPROVAL_NOT_ALLOWED`) unless they are the practice Owner. The people who "
        "prepared it are notified. Requires `invoice.approve`."
    ),
    responses=error_responses(403, 404, 409),
)
async def approve_invoice(
    invoice_id: uuid.UUID,
    org: Approver,
    service: InvoiceServiceDep,
    ctx: ContextDep,
    body: InvoiceApprove | None = None,
) -> ApiResponse[InvoiceRead]:
    approved = await service.approve(org, invoice_id, body or InvoiceApprove(), ctx)
    return ok(approved, "Invoice approved")


@router.post(
    "/invoices/{invoice_id}/reject",
    summary="Reject an invoice",
    description=(
        "Sends an invoice that is `awaiting_approval` (or `approved` but not yet issued) back "
        "to `draft`. The `reason` is required and goes to the people who prepared it (409 "
        "`INVOICE_NOT_AWAITING_APPROVAL` for any other status). Any holder of "
        "`invoice.approve` may reject, including the person who submitted it. "
        "Requires `invoice.approve`."
    ),
    responses=error_responses(403, 404, 409),
)
async def reject_invoice(
    invoice_id: uuid.UUID,
    body: InvoiceReject,
    org: Approver,
    service: InvoiceServiceDep,
    ctx: ContextDep,
) -> ApiResponse[InvoiceRead]:
    return ok(await service.reject(org, invoice_id, body, ctx), "Invoice rejected")


@router.post(
    "/invoices/{invoice_id}/issue",
    summary="Issue an invoice",
    description=(
        "Numbers the invoice from the invoice sequence (gapless), sets the issue date "
        "(default: the draft's, else today) and due date (default: issue date + client "
        "terms, else practice terms) and moves its WIP to `billed`. Works on an `approved` "
        "invoice, or on a draft that needs no approval; a draft that needs approval, or one "
        "awaiting it, answers 409 `INVOICE_APPROVAL_REQUIRED`. Needs at least one line (409 "
        "`INVOICE_EMPTY`). Requires `invoice.issue`."
    ),
    responses=error_responses(403, 404, 409),
)
async def issue_invoice(
    invoice_id: uuid.UUID,
    org: Issuer,
    service: InvoiceServiceDep,
    ctx: ContextDep,
    body: InvoiceIssue | None = None,
) -> ApiResponse[InvoiceRead]:
    issued = await service.issue(org, invoice_id, body or InvoiceIssue(), ctx)
    return ok(issued, "Invoice issued")


@router.post(
    "/invoices/{invoice_id}/void",
    summary="Void an invoice",
    description=(
        "Voids an issued invoice that has no payments (409 `INVOICE_HAS_PAYMENTS`) and no "
        "credit notes (409 `INVOICE_HAS_CREDITS`): its WIP allocations are reversed and the "
        "items return to `unbilled`. The number is kept. Requires `invoice.issue`."
    ),
    responses=error_responses(403, 404, 409),
)
async def void_invoice(
    invoice_id: uuid.UUID,
    body: InvoiceVoid,
    org: Issuer,
    service: InvoiceServiceDep,
    ctx: ContextDep,
) -> ApiResponse[InvoiceRead]:
    return ok(await service.void(org, invoice_id, body, ctx), "Invoice voided")


@router.get(
    "/invoices/{invoice_id}/pdf",
    summary="Invoice PDF",
    description=(
        "The invoice as a PDF (letterhead, client address, lines, VAT summary, totals, bank "
        "details), drawn with the client's invoice template, else the practice default: its "
        "layout (`standard`, `detailed` with the items of every line, `summary` with one row "
        "per job), `show_logo` (letterhead block), `show_bank_details` and `footer_text`. "
        "`template_id` previews another template (404 for an unknown or another practice's "
        "one, and for an inactive one without `settings.manage`). Drafts and invoices not yet "
        "issued carry a DRAFT watermark. Requires `invoice.read`."
    ),
    response_class=Response,
    responses={**error_responses(403, 404), **PDF_RESPONSE},
)
async def invoice_pdf(
    invoice_id: uuid.UUID,
    org: Reader,
    service: InvoiceServiceDep,
    template_id: Annotated[
        uuid.UUID | None, Query(description="Preview with this invoice template")
    ] = None,
) -> Response:
    filename, content = await service.pdf(org, invoice_id, template_id)
    return pdf_response(filename, content)


@router.post(
    "/invoices/{invoice_id}/send",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Email an invoice",
    description=(
        "Queues a `send_invoice_email` job that emails the PDF to the given addresses, else "
        "the client's `invoice_email`, else its billing contacts, else its primary contact, "
        "else the client email (422 `NO_RECIPIENT` when there is none); `sent_at` is set "
        "when it has gone. A "
        "second request while one is queued is not queued again (`queued: false`). Issued "
        "invoices only. Requires `invoice.issue`."
    ),
    responses=error_responses(403, 404, 409),
)
async def send_invoice(
    invoice_id: uuid.UUID,
    org: Issuer,
    service: InvoiceServiceDep,
    ctx: ContextDep,
    body: InvoiceSend | None = None,
) -> ApiResponse[InvoiceSent]:
    sent = await service.send(org, invoice_id, body or InvoiceSend(), ctx)
    return ok(sent, "Invoice email queued" if sent.queued else "Already queued")
