"""Expense receipts: upload, download, delete and the Receipts page. HTTP only: the rules live in
the service (docs/modules/expenses.md, "Receipts")."""

import uuid
from datetime import date
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from app.core.exceptions import FileUploadError, ValidationError
from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, PageParamsDep
from app.repositories.expense_receipts import ReceiptFilters
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.expenses import ClaimStatus, ReceiptListItem, ReceiptRead, ReceiptSort
from app.services.expense_receipts import ExpenseReceiptService
from app.services.receipt_files import MAX_RECEIPT_BODY_BYTES, ReceiptTooLargeError
from app.services.storage import FileStorage, get_storage
from app.utils.multipart import FormPart, parse_form_data

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Expense receipts"])

StorageDep = Annotated[FileStorage, Depends(get_storage)]


def get_receipt_service(session: SessionDep, storage: StorageDep) -> ExpenseReceiptService:
    return ExpenseReceiptService(session, storage)


ReceiptServiceDep = Annotated[ExpenseReceiptService, Depends(get_receipt_service)]


async def _read_upload(request: Request) -> FormPart:
    """The ``file`` part of an upload. The body is read under the cap of one receipt plus its
    envelope: the global body limit exempts this endpoint, and reading stops at the cap, whatever
    the client keeps sending."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_RECEIPT_BODY_BYTES:
        raise ReceiptTooLargeError()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_RECEIPT_BODY_BYTES:
            raise ReceiptTooLargeError()
    parts = parse_form_data(request.headers.get("content-type", ""), bytes(body))
    files = [p for p in parts if p.name == "file"]
    if len(files) != 1 or files[0].filename is None:
        raise FileUploadError("Send one file in a form field named 'file'.")
    if not files[0].filename.strip():
        raise FileUploadError("No file was chosen.")
    return files[0]


def _content_disposition(disposition: str, file_name: str) -> str:
    """``inline`` or ``attachment`` with the name in an ASCII form and in RFC 5987 UTF-8 form."""
    ascii_name = "".join(
        c if c.isascii() and c.isprintable() and c not in '"\\;' else "_" for c in file_name
    )
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(file_name, safe='')}"


@router.post(
    "/expenses/{line_id}/receipts",
    status_code=status.HTTP_201_CREATED,
    summary="Upload a receipt",
    description=(
        "`multipart/form-data` with the file in the field `file`. PDF, JPEG or PNG only, at most "
        "10 MiB (10,485,760 bytes; 413 `FILE_TOO_LARGE`, reading stops at the limit): the "
        "extension must be `.pdf`, `.jpg`, `.jpeg` or `.png`, the content must be what it says "
        "(magic bytes) and a content type the client declares must fit (422 "
        "`FILE_TYPE_NOT_ALLOWED`; 422 `FILE_EMPTY`). The name is sanitised and the file stored "
        "under a random key. A line holds at most 5 receipts (409 `RECEIPT_LIMIT_REACHED`). "
        "Only while the claim is a `draft` or `returned` (409 `EXPENSE_CLAIM_LOCKED`); the "
        "claimant with `expense.log`, or anyone with `expense.manage`. 503 `STORAGE_UNAVAILABLE` "
        "when the file store cannot take the file."
    ),
    responses=error_responses(400, 403, 404, 409, 413, 503),
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
                }
            },
        }
    },
)
async def upload_receipt(
    line_id: uuid.UUID,
    request: Request,
    org: OrgContextDep,
    service: ReceiptServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ReceiptRead]:
    await service.ensure_can_attach(org, line_id)
    part = await _read_upload(request)
    assert part.filename is not None
    receipt = await service.add(
        org,
        line_id,
        file_name=part.filename,
        declared_type=part.content_type,
        data=part.data,
        rctx=ctx,
    )
    return ok(receipt, "Receipt uploaded")


@router.get(
    "/expenses/{line_id}/receipts/{receipt_id}",
    summary="Download a receipt",
    description=(
        "Streams the file with `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`, the "
        "content type that was detected on upload and `Content-Disposition: inline` for images "
        "only, `attachment` for a PDF. The request needs the `Authorization` header, so fetch it "
        "and show it from a blob URL. 404 `RECEIPT_FILE_MISSING` when the store has lost the "
        "file. Whoever may see the claim."
    ),
    responses={
        **error_responses(403, 404),
        200: {"content": {"application/octet-stream": {}}, "description": "The file"},
    },
    response_class=Response,
)
async def download_receipt(
    line_id: uuid.UUID, receipt_id: uuid.UUID, org: OrgContextDep, service: ReceiptServiceDep
) -> StreamingResponse:
    receipt = await service.get(org, line_id, receipt_id)
    content = await service.open(receipt)
    inline = receipt.content_type.startswith("image/")
    return StreamingResponse(
        content,
        media_type=receipt.content_type,
        headers={
            "Content-Disposition": _content_disposition(
                "inline" if inline else "attachment", receipt.file_name
            ),
            "Content-Length": str(receipt.size_bytes),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete(
    "/expenses/{line_id}/receipts/{receipt_id}",
    summary="Delete a receipt",
    description=(
        "Removes the receipt from the line and from the file storage, while the claim is a "
        "`draft` or `returned` (409 `EXPENSE_CLAIM_LOCKED` otherwise). The claimant with "
        "`expense.log`, or anyone with `expense.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_receipt(
    line_id: uuid.UUID,
    receipt_id: uuid.UUID,
    org: OrgContextDep,
    service: ReceiptServiceDep,
    ctx: ContextDep,
) -> ApiResponse[None]:
    await service.delete(org, line_id, receipt_id, ctx)
    return ok(message="Receipt deleted")


@router.get(
    "/expense-receipts",
    summary="List receipts",
    description=(
        "Every receipt the caller may see (the same visibility as `GET /expense-claims`), "
        "cursor-paginated, each with the line (description, date, gross, client) and the claim "
        "(title, status, claimant) it proves. Filters: `claim_id`, `expense_id` (the line), "
        "`user_id`, `mine`, `client_id`, `status` (of the claim; repeatable), "
        "`date_from`/`date_to` (inclusive, on the line date) and `q` (file name, description, "
        "supplier, claim title, claimant). `sort` = `created_at` (default) | `incurred_on` | "
        "`gross`; `order` = `asc` | `desc` (default `desc`)."
    ),
    responses=error_responses(403, 404),
)
async def list_receipts(
    org: OrgContextDep,
    service: ReceiptServiceDep,
    page: PageParamsDep,
    claim_id: Annotated[uuid.UUID | None, Query(description="Receipts of this claim")] = None,
    expense_id: Annotated[uuid.UUID | None, Query(description="Receipts of this line")] = None,
    user_id: Annotated[uuid.UUID | None, Query(description="Claims of this person")] = None,
    mine: Annotated[bool, Query(description="Only the caller's own receipts")] = False,
    client_id: Annotated[uuid.UUID | None, Query(description="Lines for this client")] = None,
    statuses: Annotated[
        list[ClaimStatus] | None,
        Query(alias="status", max_length=4, description="Claim status; repeat for several"),
    ] = None,
    date_from: Annotated[date | None, Query(description="Line dated on or after")] = None,
    date_to: Annotated[date | None, Query(description="Line dated on or before")] = None,
    q: Annotated[
        str | None,
        Query(max_length=200, description="Search file name, description, supplier, claim"),
    ] = None,
    sort: Annotated[ReceiptSort, Query(description="Sort key")] = "created_at",
    order: Annotated[Literal["asc", "desc"], Query(description="Sort direction")] = "desc",
) -> ApiResponse[list[ReceiptListItem]]:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValidationError.for_field("date_to", "The end date is before the start date.")
    result = await service.list_receipts(
        org,
        ReceiptFilters(
            claim_statuses=statuses or (),
            claim_id=claim_id,
            expense_id=expense_id,
            user_id=user_id,
            client_id=client_id,
            date_from=date_from,
            date_to=date_to,
            q=q.strip() if q and q.strip() else None,
        ),
        mine=mine,
        sort=sort,
        descending=order == "desc",
        params=page,
    )
    return ok_page(result.items, result.next_cursor)
