"""Credit note routes. HTTP only: every rule lives in ``CreditNoteService``."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.v1.invoices import PDF_RESPONSE, pdf_response
from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import PageParamsDep, require
from app.repositories.credit_notes import CreditNoteFilters
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.invoices import CreditNoteCreate, CreditNoteRead
from app.services.credit_notes import CreditNoteService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Credit notes"])


def get_credit_note_service(session: SessionDep) -> CreditNoteService:
    return CreditNoteService(session)


CreditNoteServiceDep = Annotated[CreditNoteService, Depends(get_credit_note_service)]
Reader = Annotated[OrgContext, Depends(require("invoice.read"))]
Creator = Annotated[OrgContext, Depends(require("credit_note.create"))]


@router.post(
    "/invoices/{invoice_id}/credit-notes",
    status_code=status.HTTP_201_CREATED,
    summary="Raise a credit note",
    description=(
        "Issues a numbered credit note against an issued invoice (409 `INVOICE_NOT_ISSUED`). "
        "Each line has a positive net amount and a VAT rate (default: the client's default, "
        "else the practice default; 0 when not VAT-registered). The gross total may not "
        "exceed the invoice's outstanding balance (409 `CREDIT_EXCEEDS_BALANCE`). It does "
        "not reinstate WIP. Requires `credit_note.create`."
    ),
    responses=error_responses(403, 404, 409),
)
async def create_credit_note(
    invoice_id: uuid.UUID,
    body: CreditNoteCreate,
    org: Creator,
    service: CreditNoteServiceDep,
    ctx: ContextDep,
) -> ApiResponse[CreditNoteRead]:
    return ok(await service.create(org, invoice_id, body, ctx), "Credit note issued")


@router.get(
    "/credit-notes",
    summary="List credit notes",
    description=(
        "Credit notes newest first by date, cursor-paginated; filters: client, invoice, "
        "date range, `q` (number or client name). Requires `invoice.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_credit_notes(
    org: Reader,
    service: CreditNoteServiceDep,
    page: PageParamsDep,
    client_id: Annotated[uuid.UUID | None, Query(description="Only this client's")] = None,
    invoice_id: Annotated[uuid.UUID | None, Query(description="Only this invoice's")] = None,
    date_from: Annotated[date | None, Query(description="Issued on or after")] = None,
    date_to: Annotated[date | None, Query(description="Issued on or before")] = None,
    q: Annotated[str | None, Query(max_length=100, description="Number or client")] = None,
) -> ApiResponse[list[CreditNoteRead]]:
    filters = CreditNoteFilters(
        client_id=client_id,
        invoice_id=invoice_id,
        date_from=date_from,
        date_to=date_to,
        q=q.strip() if q else None,
    )
    result, items = await service.list_credit_notes(org, filters, page)
    return ok_page(items, result.next_cursor)


@router.get(
    "/credit-notes/{credit_note_id}",
    summary="Get a credit note",
    description="One credit note with its lines. Requires `invoice.read`.",
    responses=error_responses(403, 404),
)
async def get_credit_note(
    credit_note_id: uuid.UUID, org: Reader, service: CreditNoteServiceDep
) -> ApiResponse[CreditNoteRead]:
    return ok(await service.get(org, credit_note_id))


@router.get(
    "/credit-notes/{credit_note_id}/pdf",
    summary="Credit note PDF",
    description="The credit note as a PDF. Requires `invoice.read`.",
    response_class=Response,
    responses={**error_responses(403, 404), **PDF_RESPONSE},
)
async def credit_note_pdf(
    credit_note_id: uuid.UUID, org: Reader, service: CreditNoteServiceDep
) -> Response:
    filename, content = await service.pdf(org, credit_note_id)
    return pdf_response(filename, content)
