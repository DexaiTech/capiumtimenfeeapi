"""Application Testing routes: tickets, the dashboard, bulk actions, comments and attachments.
HTTP only: every rule lives in the services."""

import uuid
from datetime import date
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from app.core.exceptions import FileUploadError, ValidationError
from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.organization import OrgContextDep, PageParamsDep, require
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.ticket_dashboard import TicketDashboard
from app.schemas.tickets import (
    AttachmentRead,
    BulkAction,
    BulkResult,
    CommentCreate,
    CommentRead,
    CommentUpdate,
    TicketCreate,
    TicketDetail,
    TicketListItem,
    TicketPriority,
    TicketSort,
    TicketStatus,
    TicketUpdate,
)
from app.services import policy
from app.services.attachment_files import MAX_UPLOAD_BODY_BYTES, AttachmentTooLargeError
from app.services.policy import OrgContext
from app.services.storage import FileStorage, get_storage
from app.services.ticket_attachments import TicketAttachmentService
from app.services.ticket_comments import TicketCommentService
from app.services.ticket_dashboard import TicketDashboardService
from app.services.tickets import TicketListQuery, TicketService
from app.utils.multipart import FormPart, parse_form_data

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Tickets"])

TicketViewer = Annotated[OrgContext, Depends(require("ticket.read"))]
TicketCreator = Annotated[OrgContext, Depends(require("ticket.create"))]
TicketEditor = Annotated[OrgContext, Depends(require("ticket.update"))]
TicketDeleter = Annotated[OrgContext, Depends(require("ticket.delete"))]

StorageDep = Annotated[FileStorage, Depends(get_storage)]


async def _comment_writer(org: OrgContextDep) -> OrgContext:
    """Deleting a comment or a file needs ``ticket.update`` (own) or ``ticket.delete`` (any)."""
    policy.ensure_any(org, "ticket.update", "ticket.delete")
    return org


ContentEditor = Annotated[OrgContext, Depends(_comment_writer)]


def get_ticket_service(session: SessionDep, storage: StorageDep) -> TicketService:
    return TicketService(session, storage)


def get_comment_service(session: SessionDep, storage: StorageDep) -> TicketCommentService:
    return TicketCommentService(session, storage)


def get_attachment_service(session: SessionDep, storage: StorageDep) -> TicketAttachmentService:
    return TicketAttachmentService(session, storage)


def get_dashboard_service(session: SessionDep) -> TicketDashboardService:
    return TicketDashboardService(session)


TicketServiceDep = Annotated[TicketService, Depends(get_ticket_service)]
CommentServiceDep = Annotated[TicketCommentService, Depends(get_comment_service)]
AttachmentServiceDep = Annotated[TicketAttachmentService, Depends(get_attachment_service)]
DashboardServiceDep = Annotated[TicketDashboardService, Depends(get_dashboard_service)]


# --- Upload plumbing ---------------------------------------------------------------------


async def _read_upload(
    request: Request, comment_id_param: uuid.UUID | None
) -> tuple[FormPart, uuid.UUID | None]:
    """The ``file`` part (and the ``comment_id`` field) of an upload. The body is read under the
    cap of one file plus its envelope: the global body limit exempts this endpoint, and reading
    stops at the cap, whatever the client keeps sending."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BODY_BYTES:
        raise AttachmentTooLargeError()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD_BODY_BYTES:
            raise AttachmentTooLargeError()
    parts = parse_form_data(request.headers.get("content-type", ""), bytes(body))
    files = [p for p in parts if p.name == "file"]
    if len(files) != 1 or files[0].filename is None:
        raise FileUploadError("Send one file in a form field named 'file'.")
    if not files[0].filename.strip():
        raise FileUploadError("No file was chosen.")
    comment_id = comment_id_param
    for part in parts:
        if part.name == "comment_id" and part.filename is None:
            text = part.data.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                from_form = uuid.UUID(text)
            except ValueError as exc:
                raise ValidationError.for_field("comment_id", "Use the id of a comment.") from exc
            if comment_id is not None and comment_id != from_form:
                raise ValidationError.for_field(
                    "comment_id", "Send the comment id once (form field or query parameter)."
                )
            comment_id = from_form
    return files[0], comment_id


def _content_disposition(disposition: str, file_name: str) -> str:
    """``inline`` or ``attachment`` with the name in an ASCII form and in RFC 5987 UTF-8 form."""
    ascii_name = "".join(
        c if c.isascii() and c.isprintable() and c not in '"\\;' else "_" for c in file_name
    )
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(file_name, safe='')}"


# --- Dashboard and bulk actions (before the routes with a ticket id) --------------------


@router.get(
    "/tickets/dashboard",
    summary="Application Testing dashboard",
    description=(
        "Figures for the tickets created in the range (the practice's calendar days; "
        "`date_from`/`date_to` default to the last 19 days ending today; at most 366 days, "
        "422 otherwise): the six KPI cards (`total`, `open`, `hold`, `in_progress`, `reopen`, "
        "`closed`) each with the count, the count for the period of the same length just "
        "before the range and the `change` (`new` when that was 0 and now it is not, a whole "
        "`percent` otherwise, `none` when both are 0); tickets by status, by priority and by "
        "tag (the seven most used, then `Others`: the tickets with any other tag, each once); "
        "the status trend of the 14 days ending on `date_to` (each ticket of the range in the "
        "status it had at the end of the day, from the status history); the top assignees "
        "with an `Unassigned` row; and the seven newest tickets. Requires `ticket.read`."
    ),
    responses=error_responses(403, 404),
)
async def ticket_dashboard(
    org: TicketViewer,
    service: DashboardServiceDep,
    date_from: Annotated[date | None, Query(description="First day of the range")] = None,
    date_to: Annotated[date | None, Query(description="Last day of the range, inclusive")] = None,
) -> ApiResponse[TicketDashboard]:
    return ok(await service.dashboard(org, date_from, date_to))


@router.post(
    "/tickets/bulk",
    summary="Bulk action on tickets",
    description=(
        "Applies one `action` (`set_status`, `set_priority`, `add_tag`, `remove_tag`, `delete`) "
        "to 1-200 tickets (`value`: the status, the priority or the tag id; none for `delete`) "
        "in one transaction: all or nothing. 404 when any id is not the practice's, 422 for a "
        "wrong `value`. `affected` is the number of distinct tickets selected and `changed` how "
        "many of them actually changed (a ticket already in the wanted state does not). Each "
        "ticket that changed gets its own audit row, as if it had been changed alone. `delete` "
        "needs `ticket.delete` and removes comments and files too; the other actions need "
        "`ticket.update`."
    ),
    responses=error_responses(403, 404),
)
async def bulk_tickets(
    body: BulkAction, org: OrgContextDep, service: TicketServiceDep, ctx: ContextDep
) -> ApiResponse[BulkResult]:
    result = await service.bulk(org, body, ctx)
    verb = "deleted" if result.action == "delete" else "updated"
    return ok(result, f"{result.changed} ticket{'s' if result.changed != 1 else ''} {verb}")


# --- Tickets ----------------------------------------------------------------------------


@router.get(
    "/tickets",
    summary="List tickets",
    description=(
        "Cursor-paginated. `q` searches code, subject and description (case-insensitive); "
        "`status`, `priority` and `tag_id` may be repeated (a ticket matches when it has any of "
        "the tags); `assignee_id` or `unassigned=true`; `created_from`/`created_to` are "
        "inclusive days in the practice's time zone. `sort` = `number` (alias `code`) | "
        "`subject` | `created_at` (default) | `status` | `priority` (high first); `order` = "
        "`asc` | `desc` (default: `desc` for `created_at` and `number`, `asc` otherwise). A "
        "cursor from another sort answers 422. A page costs the same number of queries "
        "whatever its size. Requires `ticket.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_tickets(
    org: TicketViewer,
    service: TicketServiceDep,
    page: PageParamsDep,
    q: Annotated[
        str | None, Query(max_length=200, description="Search code, subject and description")
    ] = None,
    statuses: Annotated[
        list[TicketStatus] | None,
        Query(alias="status", max_length=5, description="Status; repeat for several"),
    ] = None,
    priorities: Annotated[
        list[TicketPriority] | None,
        Query(alias="priority", max_length=4, description="Priority; repeat for several"),
    ] = None,
    tag_ids: Annotated[
        list[uuid.UUID] | None,
        Query(alias="tag_id", max_length=50, description="Tag; repeat for several (any of them)"),
    ] = None,
    assignee_id: Annotated[uuid.UUID | None, Query(description="Assigned to this person")] = None,
    unassigned: Annotated[bool, Query(description="Only tickets without an assignee")] = False,
    created_from: Annotated[date | None, Query(description="Created on or after")] = None,
    created_to: Annotated[date | None, Query(description="Created on or before")] = None,
    sort: Annotated[TicketSort, Query(description="Sort key")] = "created_at",
    order: Annotated[Literal["asc", "desc"] | None, Query(description="Sort direction")] = None,
) -> ApiResponse[list[TicketListItem]]:
    query = TicketListQuery(
        q=q,
        statuses=statuses or (),
        priorities=priorities or (),
        tag_ids=tag_ids or (),
        assignee_id=assignee_id,
        unassigned=unassigned,
        created_from=created_from,
        created_to=created_to,
        sort=sort,
        order=order,
    )
    result = await service.list_tickets(org, query, page)
    return ok_page(result.items, result.next_cursor)


@router.post(
    "/tickets",
    status_code=status.HTTP_201_CREATED,
    summary="Create a ticket",
    description=(
        "`subject` (1-200 characters) is required; `description` (at most 10,000), `status` "
        "(default `open`), `priority` (default `not_set`), `tag_ids` (the practice's tags, at "
        "most 20) and `assignee_id` (an active member) are optional. The code (`APT271`) is "
        "taken from the practice's gapless ticket sequence in the same transaction, and the "
        "first status-history row is written. Returns the detail. Requires `ticket.create`."
    ),
    responses=error_responses(403, 404),
)
async def create_ticket(
    body: TicketCreate, org: TicketCreator, service: TicketServiceDep, ctx: ContextDep
) -> ApiResponse[TicketDetail]:
    return ok(await service.create(org, body, ctx), "Ticket created")


@router.get(
    "/tickets/{ticket_id}",
    summary="Get a ticket",
    description=(
        "The ticket with its full description, tags, assignee and every file (the ticket's own "
        "have `comment_id` null). Comments come from `GET /tickets/{id}/comments`. Requires "
        "`ticket.read`."
    ),
    responses=error_responses(403, 404),
)
async def get_ticket(
    ticket_id: uuid.UUID, org: TicketViewer, service: TicketServiceDep
) -> ApiResponse[TicketDetail]:
    return ok(await service.get(org, ticket_id))


@router.patch(
    "/tickets/{ticket_id}",
    summary="Update a ticket",
    description=(
        "Partial update of `subject`, `description` (null clears it), `status`, `priority`, "
        "`assignee_id` (null unassigns; an active member otherwise) and `tag_ids` (replaces the "
        "tags). Unknown fields and null for the others answer 422. A status change writes a "
        "history row and sets or clears `closed_at`; it is audited as `ticket.status_changed`, "
        "the other changes as `ticket.updated`. Requires `ticket.update`."
    ),
    responses=error_responses(403, 404),
)
async def update_ticket(
    ticket_id: uuid.UUID,
    body: TicketUpdate,
    org: TicketEditor,
    service: TicketServiceDep,
    ctx: ContextDep,
) -> ApiResponse[TicketDetail]:
    return ok(await service.update(org, ticket_id, body, ctx), "Ticket updated")


@router.delete(
    "/tickets/{ticket_id}",
    summary="Delete a ticket",
    description=(
        "Deletes the ticket with its comments, files (also from the storage), tag links and "
        "history. Requires `ticket.delete`."
    ),
    responses=error_responses(403, 404),
)
async def delete_ticket(
    ticket_id: uuid.UUID, org: TicketDeleter, service: TicketServiceDep, ctx: ContextDep
) -> ApiResponse[None]:
    await service.delete(org, ticket_id, ctx)
    return ok(message="Ticket deleted")


# --- Comments ---------------------------------------------------------------------------


@router.get(
    "/tickets/{ticket_id}/comments",
    summary="List a ticket's comments",
    description=(
        "Oldest first, cursor-paginated, each with its author and its files. Requires "
        "`ticket.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_comments(
    ticket_id: uuid.UUID, org: TicketViewer, service: CommentServiceDep, page: PageParamsDep
) -> ApiResponse[list[CommentRead]]:
    result = await service.list_comments(org, ticket_id, page)
    return ok_page(result.items, result.next_cursor)


@router.post(
    "/tickets/{ticket_id}/comments",
    status_code=status.HTTP_201_CREATED,
    summary="Add a comment",
    description=(
        "`body` (at most 5,000 characters) may be empty only when `attachment_ids` names files "
        "that were uploaded to this ticket beforehand and are not on a comment yet (422 "
        "otherwise). Requires `ticket.update`."
    ),
    responses=error_responses(403, 404),
)
async def create_comment(
    ticket_id: uuid.UUID,
    body: CommentCreate,
    org: TicketEditor,
    service: CommentServiceDep,
    ctx: ContextDep,
) -> ApiResponse[CommentRead]:
    return ok(await service.create(org, ticket_id, body, ctx), "Comment added")


@router.patch(
    "/tickets/{ticket_id}/comments/{comment_id}",
    summary="Edit a comment",
    description=(
        "Changes the text (`edited_at` is set). Only the author may, and needs `ticket.update`; "
        "the text may be empty only while the comment has files."
    ),
    responses=error_responses(403, 404),
)
async def update_comment(
    ticket_id: uuid.UUID,
    comment_id: uuid.UUID,
    body: CommentUpdate,
    org: TicketEditor,
    service: CommentServiceDep,
    ctx: ContextDep,
) -> ApiResponse[CommentRead]:
    return ok(await service.update(org, ticket_id, comment_id, body, ctx), "Comment updated")


@router.delete(
    "/tickets/{ticket_id}/comments/{comment_id}",
    summary="Delete a comment",
    description=(
        "Deletes the comment and its files (also from the storage). Only the author (with "
        "`ticket.update`) or someone with `ticket.delete` may."
    ),
    responses=error_responses(403, 404),
)
async def delete_comment(
    ticket_id: uuid.UUID,
    comment_id: uuid.UUID,
    org: ContentEditor,
    service: CommentServiceDep,
    ctx: ContextDep,
) -> ApiResponse[None]:
    await service.delete(org, ticket_id, comment_id, ctx)
    return ok(message="Comment deleted")


# --- Attachments ------------------------------------------------------------------------


@router.get(
    "/tickets/{ticket_id}/attachments",
    summary="List a ticket's files",
    description=(
        "Every file of the ticket, comments' included, oldest first. Requires `ticket.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_attachments(
    ticket_id: uuid.UUID, org: TicketViewer, service: AttachmentServiceDep
) -> ApiResponse[list[AttachmentRead]]:
    return ok(await service.list_attachments(org, ticket_id))


@router.post(
    "/tickets/{ticket_id}/attachments",
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file",
    description=(
        "`multipart/form-data` with the file in the field `file` and, optionally, the id of one "
        "of the ticket's comments in the field `comment_id` (or the query parameter). At most "
        "2 MB (413 `ATTACHMENT_TOO_LARGE`; reading stops at the limit); images (PNG, JPEG, GIF, "
        "WebP), PDF, DOC/DOCX, XLS/XLSX and CSV only: the extension must be on the list, the "
        "content must be what it says (magic bytes) and a content type the client declares "
        "must fit (422 `ATTACHMENT_TYPE_NOT_ALLOWED`; 422 `ATTACHMENT_EMPTY`). The name is "
        "sanitised and the file stored under a random key. A ticket holds at most 25 files "
        "(409 `ATTACHMENT_LIMIT_REACHED`). Requires `ticket.update`; a file on a comment needs "
        "its author or `ticket.delete`. 503 `STORAGE_UNAVAILABLE` when the file store cannot "
        "take the file."
    ),
    responses=error_responses(400, 403, 404, 409, 413, 503),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "comment_id": {"type": "string", "format": "uuid"},
                        },
                        "required": ["file"],
                    }
                }
            },
        }
    },
)
async def upload_attachment(
    ticket_id: uuid.UUID,
    request: Request,
    org: TicketEditor,
    service: AttachmentServiceDep,
    ctx: ContextDep,
    comment_id: Annotated[
        uuid.UUID | None, Query(description="Put the file on this comment (or send the form field)")
    ] = None,
) -> ApiResponse[AttachmentRead]:
    part, target = await _read_upload(request, comment_id)
    assert part.filename is not None
    attachment = await service.add(
        org,
        ticket_id,
        file_name=part.filename,
        declared_type=part.content_type,
        data=part.data,
        comment_id=target,
        rctx=ctx,
    )
    return ok(attachment, "File uploaded")


@router.get(
    "/tickets/{ticket_id}/attachments/{attachment_id}",
    summary="Download a file",
    description=(
        "Streams the file with `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`, "
        "the content type that was detected on upload and `Content-Disposition: inline` for "
        "images only, `attachment` for everything else. The request needs the `Authorization` "
        "header, so fetch it and show it from a blob URL. 404 `ATTACHMENT_FILE_MISSING` when "
        "the store has lost the file. Requires `ticket.read`."
    ),
    responses={
        **error_responses(403, 404),
        200: {"content": {"application/octet-stream": {}}, "description": "The file"},
    },
    response_class=Response,
)
async def download_attachment(
    ticket_id: uuid.UUID, attachment_id: uuid.UUID, org: TicketViewer, service: AttachmentServiceDep
) -> StreamingResponse:
    attachment = await service.get(org, ticket_id, attachment_id)
    content = await service.open(attachment)
    inline = attachment.content_type.startswith("image/")
    return StreamingResponse(
        content,
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": _content_disposition(
                "inline" if inline else "attachment", attachment.file_name
            ),
            "Content-Length": str(attachment.size_bytes),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete(
    "/tickets/{ticket_id}/attachments/{attachment_id}",
    summary="Delete a file",
    description=(
        "Removes the file from the ticket and from the storage. Only who uploaded it (with "
        "`ticket.update`) or someone with `ticket.delete` may; 409 `COMMENT_WOULD_BE_EMPTY` "
        "when it is all that is left of a comment."
    ),
    responses=error_responses(403, 404, 409),
)
async def delete_attachment(
    ticket_id: uuid.UUID,
    attachment_id: uuid.UUID,
    org: ContentEditor,
    service: AttachmentServiceDep,
    ctx: ContextDep,
) -> ApiResponse[None]:
    await service.delete(org, ticket_id, attachment_id, ctx)
    return ok(message="File deleted")
