"""Ticket attachments (docs/modules/tickets.md, "Attachments").

* An upload is validated first (size, extension, content, declared type: see
  ``attachment_files``) and only then touches the file storage and the database.
* The file is written to the storage inside the transaction that records it and removed again
  when that transaction fails, so a failed commit leaves no orphan file. Deleting an attachment
  (or its ticket or comment) removes the file once the transaction has committed. The storage is
  always addressed with the practice's id: a file is only reachable under its own practice.
* A ticket holds at most 25 files (409 ``ATTACHMENT_LIMIT_REACHED``). The count is taken under
  the ticket's row lock, so two uploads cannot both take the last place.
* Uploading needs ``ticket.update``; reading and downloading ``ticket.read``. A file is deleted
  by whoever uploaded it, or by anyone with ``ticket.delete``; a comment never ends up with
  neither text nor files (409 ``COMMENT_WOULD_BE_EMPTY``).
* A store that cannot take the file answers 503 ``STORAGE_UNAVAILABLE``; one that has lost a
  file answers 404 ``ATTACHMENT_FILE_MISSING`` on download.
"""

import uuid
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)
from app.db.models import TicketAttachment
from app.db.session import unit_of_work
from app.repositories.ticket_files import TicketFileRepository
from app.repositories.tickets import TicketRepository
from app.schemas.tickets import AttachmentRead
from app.services.attachment_files import inspect_upload
from app.services.common import record_audit
from app.services.policy import OrgContext
from app.services.storage import FileStorage, StorageError, discard, new_key
from app.services.ticket_comments import may_modify

MAX_ATTACHMENTS_PER_TICKET = 25


class TicketAttachmentService:
    def __init__(self, session: AsyncSession, storage: FileStorage) -> None:
        self.session = session
        self.storage = storage
        self.tickets = TicketRepository(session)
        self.files = TicketFileRepository(session)

    async def _read(self, org_id: uuid.UUID, rows: list[TicketAttachment]) -> list[AttachmentRead]:
        names = await self.tickets.names(org_id, [a.uploaded_by for a in rows])
        return [AttachmentRead.from_model(a, names) for a in rows]

    async def list_attachments(self, ctx: OrgContext, ticket_id: uuid.UUID) -> list[AttachmentRead]:
        """Every file of the ticket, oldest first."""
        org_id = ctx.organization_id
        if await self.tickets.code_of(org_id, ticket_id) is None:
            raise NotFoundError("Ticket not found.")
        return await self._read(org_id, await self.files.attachments_of_ticket(org_id, ticket_id))

    async def add(
        self,
        ctx: OrgContext,
        ticket_id: uuid.UUID,
        *,
        file_name: str,
        declared_type: str | None,
        data: bytes,
        comment_id: uuid.UUID | None,
        rctx: RequestContext,
    ) -> AttachmentRead:
        inspected = inspect_upload(file_name, declared_type, data)
        org_id = ctx.organization_id
        key = new_key()
        written = False
        try:
            async with unit_of_work(self.session):
                ticket = await self.tickets.get(org_id, ticket_id, lock=True)
                if ticket is None:
                    raise NotFoundError("Ticket not found.")
                if comment_id is not None:
                    comment = await self.files.get_comment(org_id, ticket_id, comment_id)
                    if comment is None:
                        raise ValidationError.for_field(
                            "comment_id", "This ticket has no such comment."
                        )
                    if not may_modify(ctx, comment.author_user_id):
                        raise AuthorizationError()
                held = await self.files.count_attachments(org_id, ticket_id)
                if held >= MAX_ATTACHMENTS_PER_TICKET:
                    raise ConflictError(
                        f"A ticket holds at most {MAX_ATTACHMENTS_PER_TICKET} files.",
                        code="ATTACHMENT_LIMIT_REACHED",
                        details={"max_attachments": MAX_ATTACHMENTS_PER_TICKET},
                    )
                # From here a failure must remove the file again (``discard`` below).
                written = True
                try:
                    await self.storage.put(org_id, key, data)
                except StorageError as exc:
                    raise ServiceUnavailableError(
                        "File storage is unavailable. Please try again shortly.",
                        code="STORAGE_UNAVAILABLE",
                    ) from exc
                attachment = await self.files.add_attachment(
                    TicketAttachment(
                        organization_id=org_id,
                        ticket_id=ticket_id,
                        comment_id=comment_id,
                        file_name=inspected.file_name,
                        content_type=inspected.content_type,
                        size_bytes=inspected.size_bytes,
                        storage_key=key,
                        sha256=inspected.sha256,
                        uploaded_by=ctx.user.id,
                    )
                )
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "ticket_attachment.added",
                    "ticket_attachment",
                    attachment.id,
                    {
                        "ticket_id": str(ticket_id),
                        "code": ticket.code,
                        "file_name": attachment.file_name,
                        "content_type": attachment.content_type,
                        "size_bytes": attachment.size_bytes,
                        "comment_id": str(comment_id) if comment_id else None,
                    },
                )
                [result] = await self._read(org_id, [attachment])
        except BaseException:
            if written:
                await discard(self.storage, org_id, [key])
            raise
        return result

    async def get(
        self, ctx: OrgContext, ticket_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> TicketAttachment:
        attachment = await self.files.get_attachment(ctx.organization_id, ticket_id, attachment_id)
        if attachment is None:
            raise NotFoundError("Attachment not found.")
        return attachment

    async def open(self, attachment: TicketAttachment) -> AsyncIterator[bytes]:
        """The file's content. 404 ``ATTACHMENT_FILE_MISSING`` when the store lost the file."""
        try:
            return await self.storage.open(attachment.organization_id, attachment.storage_key)
        except StorageError as exc:
            raise NotFoundError(
                "The file is no longer available.", code="ATTACHMENT_FILE_MISSING"
            ) from exc

    async def delete(
        self,
        ctx: OrgContext,
        ticket_id: uuid.UUID,
        attachment_id: uuid.UUID,
        rctx: RequestContext,
    ) -> None:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            attachment = await self.files.get_attachment(
                org_id, ticket_id, attachment_id, lock=True
            )
            if attachment is None:
                raise NotFoundError("Attachment not found.")
            if not may_modify(ctx, attachment.uploaded_by):
                raise AuthorizationError()
            if attachment.comment_id is not None:
                comment = await self.files.get_comment(
                    org_id, ticket_id, attachment.comment_id, lock=True
                )
                if (
                    comment is not None
                    and not comment.body
                    and await self.files.count_comment_attachments(org_id, comment.id) <= 1
                ):
                    raise ConflictError(
                        "This is the only content of its comment: delete the comment instead.",
                        code="COMMENT_WOULD_BE_EMPTY",
                    )
            key = attachment.storage_key
            meta = {
                "ticket_id": str(ticket_id),
                "code": await self.tickets.code_of(org_id, ticket_id),
                "file_name": attachment.file_name,
                "size_bytes": attachment.size_bytes,
            }
            await self.files.delete_attachment(attachment)
            await record_audit(
                self.session,
                ctx,
                rctx,
                "ticket_attachment.deleted",
                "ticket_attachment",
                attachment_id,
                meta,
            )
        await discard(self.storage, org_id, [key])
