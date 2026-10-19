"""Ticket comment and attachment queries. Every method is scoped to one organization."""

import uuid
from collections.abc import Collection

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TicketAttachment, TicketComment
from app.utils.pagination import Page, PageParams, paginate


class TicketFileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- Comments ---------------------------------------------------------------------

    async def add_comment(self, comment: TicketComment) -> TicketComment:
        self.session.add(comment)
        await self.session.flush()
        await self.session.refresh(comment)
        return comment

    async def save_comment(self, comment: TicketComment) -> TicketComment:
        await self.session.flush()
        await self.session.refresh(comment)
        return comment

    async def get_comment(
        self,
        organization_id: uuid.UUID,
        ticket_id: uuid.UUID,
        comment_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> TicketComment | None:
        stmt = (
            select(TicketComment)
            .where(
                TicketComment.organization_id == organization_id,
                TicketComment.ticket_id == ticket_id,
                TicketComment.id == comment_id,
            )
            .execution_options(populate_existing=True)
        )
        if lock:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def comments_page(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID, params: PageParams
    ) -> Page[TicketComment]:
        """Oldest first, keyset-paginated on ``(created_at, id)``."""
        stmt = select(TicketComment).where(
            TicketComment.organization_id == organization_id,
            TicketComment.ticket_id == ticket_id,
        )
        return await paginate(
            self.session,
            stmt,
            sort_column=TicketComment.created_at,
            id_column=TicketComment.id,
            params=params,
        )

    async def delete_comment(self, comment: TicketComment) -> None:
        """Delete the comment; its files' rows go with it."""
        await self.session.delete(comment)
        await self.session.flush()

    # --- Attachments ------------------------------------------------------------------

    async def add_attachment(self, attachment: TicketAttachment) -> TicketAttachment:
        self.session.add(attachment)
        await self.session.flush()
        await self.session.refresh(attachment)
        return attachment

    async def get_attachment(
        self,
        organization_id: uuid.UUID,
        ticket_id: uuid.UUID,
        attachment_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> TicketAttachment | None:
        stmt = (
            select(TicketAttachment)
            .where(
                TicketAttachment.organization_id == organization_id,
                TicketAttachment.ticket_id == ticket_id,
                TicketAttachment.id == attachment_id,
            )
            .execution_options(populate_existing=True)
        )
        if lock:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def attachments_of_ticket(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID
    ) -> list[TicketAttachment]:
        """Every file of the ticket, oldest first."""
        stmt = (
            select(TicketAttachment)
            .where(
                TicketAttachment.organization_id == organization_id,
                TicketAttachment.ticket_id == ticket_id,
            )
            .order_by(TicketAttachment.created_at, TicketAttachment.id)
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def attachments_of_comments(
        self, organization_id: uuid.UUID, comment_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, list[TicketAttachment]]:
        if not comment_ids:
            return {}
        stmt = (
            select(TicketAttachment)
            .where(
                TicketAttachment.organization_id == organization_id,
                TicketAttachment.comment_id.in_(comment_ids),
            )
            .order_by(TicketAttachment.created_at, TicketAttachment.id)
            .execution_options(populate_existing=True)
        )
        found: dict[uuid.UUID, list[TicketAttachment]] = {}
        for attachment in (await self.session.execute(stmt)).scalars():
            if attachment.comment_id is not None:
                found.setdefault(attachment.comment_id, []).append(attachment)
        return found

    async def loose_attachments(
        self,
        organization_id: uuid.UUID,
        ticket_id: uuid.UUID,
        attachment_ids: Collection[uuid.UUID],
    ) -> list[TicketAttachment]:
        """Those of the ticket's files, among ``attachment_ids``, that are on no comment yet."""
        if not attachment_ids:
            return []
        stmt = (
            select(TicketAttachment)
            .where(
                TicketAttachment.organization_id == organization_id,
                TicketAttachment.ticket_id == ticket_id,
                TicketAttachment.comment_id.is_(None),
                TicketAttachment.id.in_(attachment_ids),
            )
            .order_by(TicketAttachment.created_at, TicketAttachment.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def count_attachments(self, organization_id: uuid.UUID, ticket_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(
            TicketAttachment.organization_id == organization_id,
            TicketAttachment.ticket_id == ticket_id,
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def count_comment_attachments(
        self, organization_id: uuid.UUID, comment_id: uuid.UUID
    ) -> int:
        stmt = select(func.count()).where(
            TicketAttachment.organization_id == organization_id,
            TicketAttachment.comment_id == comment_id,
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def keys_of_comment(self, organization_id: uuid.UUID, comment_id: uuid.UUID) -> list[str]:
        stmt = select(TicketAttachment.storage_key).where(
            TicketAttachment.organization_id == organization_id,
            TicketAttachment.comment_id == comment_id,
        )
        return list((await self.session.execute(stmt)).scalars())

    async def delete_attachment(self, attachment: TicketAttachment) -> None:
        await self.session.delete(attachment)
        await self.session.flush()
