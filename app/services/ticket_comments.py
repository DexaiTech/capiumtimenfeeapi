"""Ticket comments (docs/modules/tickets.md, "Comments").

* A comment has a body of at most 5,000 characters; the body may be empty only when the comment
  carries files. Files are put on a comment either by uploading them to the ticket first and
  naming them in ``attachment_ids``, or by uploading with ``comment_id``.
* Writing needs ``ticket.update``. Only the author edits a comment; the author deletes their
  own, and anyone with ``ticket.delete`` deletes any (moderation). Deleting a comment removes
  its files from the storage once the transaction has committed.
* Reading needs ``ticket.read`` (checked by the router).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import AuthorizationError, NotFoundError, ValidationError
from app.db.models import TicketComment
from app.db.session import unit_of_work
from app.repositories.ticket_files import TicketFileRepository
from app.repositories.tickets import TicketRepository
from app.schemas.tickets import CommentCreate, CommentRead, CommentUpdate
from app.services import policy
from app.services.common import audit_excerpt, record_audit
from app.services.policy import OrgContext
from app.services.storage import FileStorage, discard
from app.services.ticket_reader import TicketReader
from app.utils.pagination import Page, PageParams


def may_modify(ctx: OrgContext, author_id: uuid.UUID) -> bool:
    """Whether the caller may change what ``author_id`` wrote or uploaded."""
    if policy.has_permission(ctx, "ticket.delete"):
        return True
    return policy.has_permission(ctx, "ticket.update") and author_id == ctx.user.id


class TicketCommentService:
    def __init__(self, session: AsyncSession, storage: FileStorage) -> None:
        self.session = session
        self.storage = storage
        self.tickets = TicketRepository(session)
        self.files = TicketFileRepository(session)
        self.reader = TicketReader(session)

    async def list_comments(
        self, ctx: OrgContext, ticket_id: uuid.UUID, page: PageParams
    ) -> Page[CommentRead]:
        """Oldest first."""
        org_id = ctx.organization_id
        if await self.tickets.code_of(org_id, ticket_id) is None:
            raise NotFoundError("Ticket not found.")
        found = await self.files.comments_page(org_id, ticket_id, page)
        return Page(
            items=await self.reader.comments(org_id, found.items), next_cursor=found.next_cursor
        )

    async def create(
        self, ctx: OrgContext, ticket_id: uuid.UUID, data: CommentCreate, rctx: RequestContext
    ) -> CommentRead:
        org_id = ctx.organization_id
        attachment_ids = list(dict.fromkeys(data.attachment_ids))
        async with unit_of_work(self.session):
            code = await self.tickets.code_of(org_id, ticket_id)
            if code is None:
                raise NotFoundError("Ticket not found.")
            loose = await self.files.loose_attachments(org_id, ticket_id, attachment_ids)
            if len(loose) != len(attachment_ids):
                raise ValidationError.for_field(
                    "attachment_ids",
                    "Use only files uploaded to this ticket that are not on a comment yet.",
                )
            if not data.body and not loose:
                raise ValidationError.for_field(
                    "body", "Write a comment or attach a file.", "required"
                )
            comment = await self.files.add_comment(
                TicketComment(
                    organization_id=org_id,
                    ticket_id=ticket_id,
                    author_user_id=ctx.user.id,
                    body=data.body,
                    body_html=data.body_html,
                )
            )
            for attachment in loose:
                attachment.comment_id = comment.id
            await self.session.flush()
            await record_audit(
                self.session,
                ctx,
                rctx,
                "ticket_comment.created",
                "ticket_comment",
                comment.id,
                {"ticket_id": str(ticket_id), "code": code, "attachments": len(loose)},
            )
            [result] = await self.reader.comments(org_id, [comment])
        return result

    async def update(
        self,
        ctx: OrgContext,
        ticket_id: uuid.UUID,
        comment_id: uuid.UUID,
        data: CommentUpdate,
        rctx: RequestContext,
    ) -> CommentRead:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            comment = await self.files.get_comment(org_id, ticket_id, comment_id, lock=True)
            if comment is None:
                raise NotFoundError("Comment not found.")
            if comment.author_user_id != ctx.user.id:
                raise AuthorizationError("Only the author can edit a comment.")
            if (data.body, data.body_html) != (comment.body, comment.body_html):
                if not data.body and not await self.files.count_comment_attachments(
                    org_id, comment.id
                ):
                    raise ValidationError.for_field(
                        "body", "A comment without files needs some text.", "required"
                    )
                old = comment.body
                comment.body = data.body
                comment.body_html = data.body_html
                comment.edited_at = datetime.now(UTC)
                comment = await self.files.save_comment(comment)
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "ticket_comment.updated",
                    "ticket_comment",
                    comment.id,
                    {
                        "ticket_id": str(ticket_id),
                        "code": await self.tickets.code_of(org_id, ticket_id),
                        "changes": {"body": [audit_excerpt(old), audit_excerpt(comment.body)]},
                    },
                )
            [result] = await self.reader.comments(org_id, [comment])
        return result

    async def delete(
        self,
        ctx: OrgContext,
        ticket_id: uuid.UUID,
        comment_id: uuid.UUID,
        rctx: RequestContext,
    ) -> None:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            comment = await self.files.get_comment(org_id, ticket_id, comment_id, lock=True)
            if comment is None:
                raise NotFoundError("Comment not found.")
            if not may_modify(ctx, comment.author_user_id):
                raise AuthorizationError()
            keys = await self.files.keys_of_comment(org_id, comment.id)
            code = await self.tickets.code_of(org_id, ticket_id)
            await self.files.delete_comment(comment)
            await record_audit(
                self.session,
                ctx,
                rctx,
                "ticket_comment.deleted",
                "ticket_comment",
                comment_id,
                {"ticket_id": str(ticket_id), "code": code, "attachments": len(keys)},
            )
        await discard(self.storage, org_id, keys)
