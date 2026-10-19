"""Ticket tags: the practice's labels (docs/modules/tickets.md, "Tags").

* A name is unique per practice, ignoring case (409 ``TAG_NAME_TAKEN``); at most ``MAX_TAGS``
  tags exist (409 ``TAG_LIMIT_REACHED``), which keeps the list a bounded one.
* A tag that tickets carry is deleted only with ``force``: 409 ``TAG_IN_USE`` otherwise. Deleting
  it then removes it from those tickets.
* Reading needs ``ticket.read``, creating and changing ``ticket.update``, deleting
  ``ticket.delete`` (checked by the router).
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError
from app.db.models import TicketTag
from app.db.session import unit_of_work
from app.repositories.ticket_tags import TicketTagRepository
from app.schemas.tickets import TagCreate, TagDeleted, TagRead, TagUpdate
from app.services.common import apply_changes, duplicate_error, record_audit
from app.services.policy import OrgContext

MAX_TAGS = 200


def _read(tag: TicketTag, ticket_count: int) -> TagRead:
    return TagRead.model_validate(
        {
            "id": tag.id,
            "name": tag.name,
            "color": tag.color,
            "ticket_count": ticket_count,
            "created_at": tag.created_at,
            "updated_at": tag.updated_at,
        }
    )


class TicketTagService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.tags = TicketTagRepository(session)

    async def list_tags(self, ctx: OrgContext) -> list[TagRead]:
        return [
            _read(tag, used) for tag, used in await self.tags.list_with_counts(ctx.organization_id)
        ]

    async def create(self, ctx: OrgContext, data: TagCreate, rctx: RequestContext) -> TagRead:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            if await self.tags.count(org_id) >= MAX_TAGS:
                raise ConflictError(
                    f"A practice can have at most {MAX_TAGS} tags.", code="TAG_LIMIT_REACHED"
                )
            await self._ensure_name_free(org_id, data.name, exclude_id=None)
            tag = await self.tags.add(
                TicketTag(organization_id=org_id, name=data.name, color=data.color)
            )
            await record_audit(
                self.session,
                ctx,
                rctx,
                "ticket_tag.created",
                "ticket_tag",
                tag.id,
                {"name": tag.name, "color": tag.color},
            )
            result = _read(tag, 0)
        return result

    async def update(
        self, ctx: OrgContext, tag_id: uuid.UUID, data: TagUpdate, rctx: RequestContext
    ) -> TagRead:
        org_id = ctx.organization_id
        values = data.model_dump(exclude_unset=True)
        async with unit_of_work(self.session):
            tag = await self.tags.get(org_id, tag_id, lock=True)
            if tag is None:
                raise NotFoundError("Tag not found.")
            if "name" in values:
                await self._ensure_name_free(org_id, values["name"], exclude_id=tag.id)
            changes = apply_changes(tag, values)
            tag = await self.tags.save(tag)
            if changes:
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "ticket_tag.updated",
                    "ticket_tag",
                    tag.id,
                    {"name": tag.name, "changes": changes},
                )
            result = _read(tag, await self.tags.usage(org_id, tag.id))
        return result

    async def delete(
        self, ctx: OrgContext, tag_id: uuid.UUID, *, force: bool, rctx: RequestContext
    ) -> TagDeleted:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            tag = await self.tags.get(org_id, tag_id, lock=True)
            if tag is None:
                raise NotFoundError("Tag not found.")
            used = await self.tags.usage(org_id, tag.id)
            if used and not force:
                raise ConflictError(
                    f"This tag is on {used} ticket{'s' if used != 1 else ''}. Delete it "
                    "anyway to take it off them.",
                    code="TAG_IN_USE",
                    details={"ticket_count": used},
                )
            meta = {"name": tag.name, "color": tag.color, "tickets_untagged": used}
            await self.tags.delete(tag)
            await record_audit(
                self.session, ctx, rctx, "ticket_tag.deleted", "ticket_tag", tag_id, meta
            )
        return TagDeleted(id=tag_id, tickets_untagged=used)

    async def _ensure_name_free(
        self, org_id: uuid.UUID, name: str, *, exclude_id: uuid.UUID | None
    ) -> None:
        if await self.tags.name_taken(org_id, name, exclude_id=exclude_id):
            raise duplicate_error("name", "A tag with this name already exists.", "TAG_NAME_TAKEN")
