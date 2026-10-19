"""What a credit note does to WIP: return the credited work to WIP or write it off. HTTP only; the
rules live in ``CreditNoteWipService``."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import require
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.wip import CreditNoteWipCreate, CreditNoteWipRead
from app.services.credit_note_wip import CreditNoteWipService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Credit notes"])


def get_credit_note_wip_service(session: SessionDep) -> CreditNoteWipService:
    return CreditNoteWipService(session)


CreditNoteWipServiceDep = Annotated[CreditNoteWipService, Depends(get_credit_note_wip_service)]
CreditNoteRaiser = Annotated[OrgContext, Depends(require("credit_note.create"))]
InvoiceReader = Annotated[OrgContext, Depends(require("invoice.read"))]


@router.post(
    "/credit-notes/{credit_note_id}/wip",
    status_code=status.HTTP_201_CREATED,
    summary="Return or write off the WIP of a credit note",
    description=(
        "Decides, once per credit note, what happens to the work in progress it credits. "
        "`return` puts the credited work back into WIP: whole items of the invoice, in the "
        "order of its lines, as long as their billed value fits in the credited net (a credit "
        "that completes the invoice's credit returns every item). Their allocations are "
        "released and the items are `unbilled` again; a credit that fits no whole item answers "
        "409 `NOTHING_TO_RETURN`. `write_off` leaves the items billed and records the credited "
        "net as a WIP write-off. The reason is required. Only a credit note on an issued "
        "invoice qualifies (409 `INVOICE_NOT_ISSUED`) and only once (409 "
        "`CREDIT_NOTE_WIP_DONE`). The WIP period lock applies to today. Audited "
        "(`credit_note.wip_returned` or `credit_note.wip_written_off`). Requires "
        "`credit_note.create`."
    ),
    response_model=ApiResponse[CreditNoteWipRead],
    responses=error_responses(403, 404, 409),
)
async def create_credit_note_wip(
    credit_note_id: uuid.UUID,
    body: CreditNoteWipCreate,
    org: CreditNoteRaiser,
    service: CreditNoteWipServiceDep,
    ctx: ContextDep,
) -> ApiResponse[CreditNoteWipRead]:
    done = await service.create(org, credit_note_id, body, ctx)
    message = "WIP returned" if body.mode == "return" else "Credit written off"
    return ok(done, message)


@router.get(
    "/credit-notes/{credit_note_id}/wip",
    summary="What was done with the WIP of a credit note",
    description=(
        "The WIP record of a credit note: the mode, the reason, the credited net, the WIP "
        "value that came back or was written off and the items returned. 404 until the credit "
        "note has been dealt with. Requires `invoice.read`."
    ),
    response_model=ApiResponse[CreditNoteWipRead],
    responses=error_responses(403, 404),
)
async def get_credit_note_wip(
    credit_note_id: uuid.UUID, org: InvoiceReader, service: CreditNoteWipServiceDep
) -> ApiResponse[CreditNoteWipRead]:
    return ok(await service.get(org, credit_note_id))
