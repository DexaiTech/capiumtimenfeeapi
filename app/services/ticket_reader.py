"""The read models of tickets and comments.

A page of tickets needs their tags, the names of the people and (for the detail) the files:
each is one batched query, whatever the size of the page.
"""

import uuid
from collections.abc import Collection, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.db.models import TicketComment, TicketTag
from app.repositories.ticket_files import TicketFileRepository
from app.repositories.tickets import TicketRepository, TicketRow
from app.schemas.tickets import (
    AttachmentRead,
    CommentRead,
    TagRef,
    TicketDetail,
    TicketListItem,
    TicketPerson,
)


def _person(user_id: uuid.UUID, names: dict[uuid.UUID, str]) -> TicketPerson:
    return TicketPerson(id=user_id, full_name=names.get(user_id, ""))


def _base(row: TicketRow, tags: list[TicketTag], names: dict[uuid.UUID, str]) -> dict[str, Any]:
    ticket = row.ticket
    return {
        "id": ticket.id,
        "code": ticket.code,
        "number": ticket.number,
        "category": ticket.category,
        "subject": ticket.subject,
        "status": ticket.status,
        "priority": ticket.priority,
        "tags": [TagRef.from_model(tag) for tag in tags],
        "assignee": _person(ticket.assignee_user_id, names) if ticket.assignee_user_id else None,
        "created_by": ticket.created_by,
        "created_by_name": names.get(ticket.created_by),
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
        "closed_at": ticket.closed_at,
        "comment_count": row.comment_count,
        "attachment_count": row.attachment_count,
    }


class TicketReader:
    def __init__(self, session: AsyncSession) -> None:
        self.tickets = TicketRepository(session)
        self.files = TicketFileRepository(session)

    async def items(
        self, organization_id: uuid.UUID, rows: Sequence[TicketRow]
    ) -> list[TicketListItem]:
        """List items for rows of ``ticket_rows_select``: two more queries for the whole page."""
        if not rows:
            return []
        tags = await self.tickets.tags_for(organization_id, [r.ticket.id for r in rows])
        names = await self.tickets.names(
            organization_id,
            [i for r in rows for i in (r.ticket.assignee_user_id, r.ticket.created_by)],
        )
        return [
            TicketListItem.model_validate(
                {**_base(r, tags.get(r.ticket.id, []), names), "description_excerpt": r.excerpt}
            )
            for r in rows
        ]

    async def detail(self, organization_id: uuid.UUID, ticket_id: uuid.UUID) -> TicketDetail:
        row = await self.tickets.get_row(organization_id, ticket_id)
        if row is None:
            raise NotFoundError("Ticket not found.")
        tags = await self.tickets.tags_for(organization_id, [ticket_id])
        attachments = await self.files.attachments_of_ticket(organization_id, ticket_id)
        names = await self.tickets.names(
            organization_id,
            [row.ticket.assignee_user_id, row.ticket.created_by]
            + [a.uploaded_by for a in attachments],
        )
        return TicketDetail.model_validate(
            {
                **_base(row, tags.get(ticket_id, []), names),
                "description": row.ticket.description,
                "attachments": [AttachmentRead.from_model(a, names) for a in attachments],
            }
        )

    async def comments(
        self, organization_id: uuid.UUID, comments: Collection[TicketComment]
    ) -> list[CommentRead]:
        """Comment read models, with their files and the names of authors and uploaders."""
        if not comments:
            return []
        files = await self.files.attachments_of_comments(organization_id, [c.id for c in comments])
        uploaders = [a.uploaded_by for group in files.values() for a in group]
        names = await self.tickets.names(
            organization_id, [c.author_user_id for c in comments] + uploaders
        )
        return [
            CommentRead(
                id=c.id,
                ticket_id=c.ticket_id,
                author=_person(c.author_user_id, names),
                body=c.body,
                edited_at=c.edited_at,
                created_at=c.created_at,
                attachments=[AttachmentRead.from_model(a, names) for a in files.get(c.id, [])],
            )
            for c in comments
        ]
