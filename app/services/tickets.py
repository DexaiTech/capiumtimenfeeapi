"""Tickets: the practice's own bug and enhancement tracker (docs/modules/tickets.md).

* A ticket's number comes from the gapless ``ticket`` sequence, taken in the transaction that
  creates the ticket, and its code is the practice's prefix and that number (``APT271``).
* Creating a ticket and every change of its status write a ``ticket_status_history`` row in the
  same transaction; ``closed_at`` is set exactly while the status is ``closed``.
* The assignee must be an active member of the practice and the tags must be the practice's.
* Reading needs ``ticket.read``, creating ``ticket.create``, changing ``ticket.update`` and
  deleting ``ticket.delete`` (a bulk delete too, the other bulk actions need ``ticket.update``);
  the router checks the single-ticket rules, ``bulk`` checks its own.
* Every write is audited: ``ticket.created``, ``ticket.updated``, ``ticket.status_changed`` and
  ``ticket.deleted``. A bulk action writes the same row for each ticket it changes, marked with
  ``bulk`` in its meta, so a ticket's trail is complete whichever way it was changed.
* Deleting a ticket removes its comments, files and history; the files leave the storage once
  the transaction has committed.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError, ValidationError
from app.db.models import Ticket, TicketTag
from app.db.session import unit_of_work
from app.repositories.ticket_tags import TicketTagRepository
from app.repositories.tickets import (
    TicketFilters,
    TicketRepository,
    history_entry,
    normalise_sort,
)
from app.schemas.tickets import (
    BulkAction,
    BulkResult,
    TicketCreate,
    TicketDetail,
    TicketListItem,
    TicketUpdate,
)
from app.services import policy
from app.services.audit import AuditRow
from app.services.common import audit_excerpt, plain, record_audit, record_audit_many
from app.services.policy import OrgContext
from app.services.sequences import take_number
from app.services.storage import FileStorage, discard
from app.services.ticket_reader import TicketReader
from app.utils.pagination import Page, PageParams
from app.utils.practice_dates import end_of_day_utc, start_of_day_utc

_UPDATABLE = ("subject", "description", "priority")
# These sorts read best newest (highest) first when the caller does not say.
_DESCENDING_BY_DEFAULT = frozenset({"created_at", "number"})


@dataclass(frozen=True, slots=True)
class TicketListQuery:
    q: str | None = None
    statuses: Sequence[str] = ()
    priorities: Sequence[str] = ()
    tag_ids: Sequence[uuid.UUID] = ()
    assignee_id: uuid.UUID | None = None
    unassigned: bool = False
    created_from: date | None = None
    created_to: date | None = None
    sort: str = "created_at"
    order: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


class TicketService:
    def __init__(self, session: AsyncSession, storage: FileStorage) -> None:
        self.session = session
        self.storage = storage
        self.tickets = TicketRepository(session)
        self.tags = TicketTagRepository(session)
        self.reader = TicketReader(session)

    # --- Reads ----------------------------------------------------------------------------

    async def list_tickets(
        self, ctx: OrgContext, query: TicketListQuery, page: PageParams
    ) -> Page[TicketListItem]:
        if query.assignee_id is not None and query.unassigned:
            raise ValidationError.for_field(
                "unassigned", "Use either assignee_id or unassigned, not both."
            )
        if query.created_from and query.created_to and query.created_from > query.created_to:
            raise ValidationError.for_field("created_from", "The start cannot be after the end.")
        zone = ctx.organization.timezone
        search = (query.q or "").replace("\x00", "").strip()
        filters = TicketFilters(
            q=search or None,
            statuses=query.statuses,
            priorities=query.priorities,
            tag_ids=query.tag_ids,
            assignee_id=query.assignee_id,
            unassigned=query.unassigned,
            created_from=start_of_day_utc(query.created_from, zone) if query.created_from else None,
            created_to=end_of_day_utc(query.created_to, zone) if query.created_to else None,
        )
        sort = normalise_sort(query.sort)
        descending = query.order == "desc" if query.order else sort in _DESCENDING_BY_DEFAULT
        found = await self.tickets.list_page(
            ctx.organization_id,
            filters,
            sort=sort,
            descending=descending,
            limit=page.limit,
            cursor=page.cursor,
        )
        items = await self.reader.items(ctx.organization_id, found.rows)
        return Page(items=items, next_cursor=found.next_cursor)

    async def get(self, ctx: OrgContext, ticket_id: uuid.UUID) -> TicketDetail:
        return await self.reader.detail(ctx.organization_id, ticket_id)

    # --- Writes ---------------------------------------------------------------------------

    async def create(
        self, ctx: OrgContext, data: TicketCreate, rctx: RequestContext
    ) -> TicketDetail:
        org_id = ctx.organization_id
        tag_ids = list(dict.fromkeys(data.tag_ids))
        async with unit_of_work(self.session):
            tags = await self._load_tags(org_id, tag_ids)
            await self._ensure_assignee(org_id, data.assignee_id)
            # The number is taken last and the creation time after it: the sequence row stays
            # locked until the commit, so tickets are numbered in the order they were created.
            number, code = await take_number(self.session, org_id, "ticket")
            now = _now()
            ticket = await self.tickets.add(
                Ticket(
                    organization_id=org_id,
                    number=number,
                    code=code,
                    subject=data.subject,
                    description=data.description,
                    status=data.status,
                    priority=data.priority,
                    assignee_user_id=data.assignee_id,
                    created_by=ctx.user.id,
                    created_at=now,
                    updated_at=now,
                    closed_at=now if data.status == "closed" else None,
                )
            )
            await self.tickets.link_tags(org_id, [ticket.id], [t.id for t in tags])
            await self.tickets.add_history(
                [
                    history_entry(
                        org_id,
                        ticket.id,
                        from_status=None,
                        to_status=ticket.status,
                        changed_by=ctx.user.id,
                        changed_at=now,
                    )
                ]
            )
            await record_audit(
                self.session,
                ctx,
                rctx,
                "ticket.created",
                "ticket",
                ticket.id,
                {
                    "code": ticket.code,
                    "subject": ticket.subject,
                    "status": ticket.status,
                    "priority": ticket.priority,
                    "assignee_id": plain(ticket.assignee_user_id),
                    "tags": sorted(t.name for t in tags),
                },
            )
            result = await self.reader.detail(org_id, ticket.id)
        return result

    async def update(
        self, ctx: OrgContext, ticket_id: uuid.UUID, data: TicketUpdate, rctx: RequestContext
    ) -> TicketDetail:
        fields = data.model_fields_set
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            ticket = await self.tickets.get(org_id, ticket_id, lock=True)
            if ticket is None:
                raise NotFoundError("Ticket not found.")
            new_assignee = data.assignee_id if "assignee_id" in fields else ticket.assignee_user_id
            if new_assignee != ticket.assignee_user_id:
                await self._ensure_assignee(org_id, new_assignee)
            wanted_tags = (
                await self._load_tags(org_id, list(dict.fromkeys(data.tag_ids or [])))
                if "tag_ids" in fields
                else None
            )
            now = _now()

            changes: dict[str, list[Any]] = {}
            for field in _UPDATABLE:
                if field not in fields:
                    continue
                new, old = getattr(data, field), getattr(ticket, field)
                if new != old:
                    changes[field] = [audit_excerpt(old), audit_excerpt(new)]
                    setattr(ticket, field, new)
            if new_assignee != ticket.assignee_user_id:
                changes["assignee_id"] = [plain(ticket.assignee_user_id), plain(new_assignee)]
                ticket.assignee_user_id = new_assignee
            if wanted_tags is not None:
                tag_change = await self._replace_tags(org_id, ticket.id, wanted_tags)
                if tag_change:
                    changes["tags"] = tag_change

            old_status = ticket.status
            new_status = data.status if "status" in fields and data.status else old_status
            if new_status != old_status:
                self._move_status(ticket, new_status, now)
                await self.tickets.add_history(
                    [
                        history_entry(
                            org_id,
                            ticket.id,
                            from_status=old_status,
                            to_status=new_status,
                            changed_by=ctx.user.id,
                            changed_at=now,
                        )
                    ]
                )
            await self.session.flush()

            meta = {"code": ticket.code, "subject": ticket.subject}
            if changes:
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "ticket.updated",
                    "ticket",
                    ticket.id,
                    {**meta, "changes": changes},
                )
            if new_status != old_status:
                await record_audit(
                    self.session,
                    ctx,
                    rctx,
                    "ticket.status_changed",
                    "ticket",
                    ticket.id,
                    {
                        **meta,
                        "from": old_status,
                        "to": new_status,
                        "changes": {"status": [old_status, new_status]},
                    },
                )
            result = await self.reader.detail(org_id, ticket.id)
        return result

    async def delete(self, ctx: OrgContext, ticket_id: uuid.UUID, rctx: RequestContext) -> None:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            ticket = await self.tickets.get(org_id, ticket_id, lock=True)
            if ticket is None:
                raise NotFoundError("Ticket not found.")
            row = await self.tickets.get_row(org_id, ticket_id)
            assert row is not None
            keys = await self.tickets.attachment_keys(org_id, [ticket_id])
            meta = {
                "code": ticket.code,
                "subject": ticket.subject,
                "status": ticket.status,
                "comments": row.comment_count,
                "attachments": row.attachment_count,
            }
            await self.tickets.delete_many(org_id, [ticket_id])
            await record_audit(self.session, ctx, rctx, "ticket.deleted", "ticket", ticket_id, meta)
        await discard(self.storage, org_id, keys)

    async def bulk(self, ctx: OrgContext, data: BulkAction, rctx: RequestContext) -> BulkResult:
        """One action on up to 200 tickets, all or nothing."""
        policy.ensure(ctx, "ticket.delete" if data.action == "delete" else "ticket.update")
        org_id = ctx.organization_id
        ids = list(dict.fromkeys(data.ticket_ids))
        keys: list[str] = []
        async with unit_of_work(self.session):
            tickets = await self.tickets.lock_many(org_id, ids)
            if len(tickets) != len(ids):
                found = {t.id for t in tickets}
                raise NotFoundError(
                    "Some of the tickets were not found.",
                    details={"ticket_ids": [str(i) for i in ids if i not in found]},
                )
            rows: list[AuditRow]
            match data.action:
                case "set_status":
                    assert data.value is not None
                    rows = await self._bulk_status(ctx, tickets, data.value)
                case "set_priority":
                    assert data.value is not None
                    rows = self._bulk_priority(tickets, data.value)
                case "add_tag" | "remove_tag":
                    tag = await self.tags.get(org_id, data.tag_id)
                    if tag is None:
                        raise ValidationError.for_field("value", "This tag does not exist.")
                    rows = await self._bulk_tag(org_id, tickets, tag, add=data.action == "add_tag")
                case "delete":
                    keys = await self.tickets.attachment_keys(org_id, ids)
                    rows = [
                        (
                            "ticket.deleted",
                            "ticket",
                            t.id,
                            {
                                "code": t.code,
                                "subject": t.subject,
                                "status": t.status,
                                "bulk": True,
                            },
                        )
                        for t in tickets
                    ]
                    await self.tickets.delete_many(org_id, ids)
            await self.session.flush()
            await record_audit_many(self.session, ctx, rctx, rows)
        await discard(self.storage, org_id, keys)
        return BulkResult(action=data.action, affected=len(ids), changed=len(rows))

    # --- Bulk actions: each returns the audit row of every ticket it changed -----------------

    async def _bulk_status(
        self, ctx: OrgContext, tickets: Sequence[Ticket], status: str
    ) -> list[AuditRow]:
        now = _now()
        moving = [t for t in tickets if t.status != status]
        history = []
        rows: list[AuditRow] = []
        for ticket in moving:
            old = ticket.status
            history.append(
                history_entry(
                    ctx.organization_id,
                    ticket.id,
                    from_status=old,
                    to_status=status,
                    changed_by=ctx.user.id,
                    changed_at=now,
                )
            )
            self._move_status(ticket, status, now)
            rows.append(
                (
                    "ticket.status_changed",
                    "ticket",
                    ticket.id,
                    {
                        "code": ticket.code,
                        "subject": ticket.subject,
                        "from": old,
                        "to": status,
                        "changes": {"status": [old, status]},
                        "bulk": True,
                    },
                )
            )
        await self.tickets.add_history(history)
        return rows

    @staticmethod
    def _bulk_priority(tickets: Sequence[Ticket], priority: str) -> list[AuditRow]:
        rows: list[AuditRow] = []
        for ticket in tickets:
            if ticket.priority == priority:
                continue
            rows.append(
                (
                    "ticket.updated",
                    "ticket",
                    ticket.id,
                    {
                        "code": ticket.code,
                        "subject": ticket.subject,
                        "changes": {"priority": [ticket.priority, priority]},
                        "bulk": True,
                    },
                )
            )
            ticket.priority = priority
        return rows

    async def _bulk_tag(
        self, org_id: uuid.UUID, tickets: Sequence[Ticket], tag: TicketTag, *, add: bool
    ) -> list[AuditRow]:
        carrying = await self.tickets.ticket_ids_with_tag(org_id, [t.id for t in tickets], tag.id)
        moving = [t for t in tickets if (t.id in carrying) != add]
        if not moving:
            return []
        moving_ids = [t.id for t in moving]
        before = await self.tickets.tags_for(org_id, moving_ids)
        if add:
            await self.tickets.link_tags(org_id, moving_ids, [tag.id])
        else:
            await self.tickets.unlink_tags(org_id, moving_ids, [tag.id])
        rows: list[AuditRow] = []
        for ticket in moving:
            old = sorted(t.name for t in before.get(ticket.id, []))
            new = sorted([*old, tag.name]) if add else [name for name in old if name != tag.name]
            rows.append(
                (
                    "ticket.updated",
                    "ticket",
                    ticket.id,
                    {
                        "code": ticket.code,
                        "subject": ticket.subject,
                        "changes": {"tags": [old, new]},
                        "bulk": True,
                    },
                )
            )
        return rows

    # --- Internals ------------------------------------------------------------------------

    @staticmethod
    def _move_status(ticket: Ticket, status: str, at: datetime) -> None:
        ticket.status = status
        ticket.closed_at = at if status == "closed" else None

    async def _load_tags(self, org_id: uuid.UUID, tag_ids: list[uuid.UUID]) -> list[TicketTag]:
        tags = await self.tags.get_many(org_id, tag_ids)
        if len(tags) != len(tag_ids):
            raise ValidationError.for_field("tag_ids", "One or more of the tags do not exist.")
        return tags

    async def _ensure_assignee(self, org_id: uuid.UUID, user_id: uuid.UUID | None) -> None:
        if user_id is not None and not await self.tickets.is_active_member(org_id, user_id):
            raise ValidationError.for_field(
                "assignee_id", "The assignee must be an active member of this practice."
            )

    async def _replace_tags(
        self, org_id: uuid.UUID, ticket_id: uuid.UUID, wanted: list[TicketTag]
    ) -> list[list[str]] | None:
        """Make ``wanted`` the ticket's tags; ``[old names, new names]`` when that changed them."""
        current_ids = set(await self.tickets.tag_ids_of(org_id, ticket_id))
        wanted_ids = {t.id for t in wanted}
        if current_ids == wanted_ids:
            return None
        current = await self.tags.get_many(org_id, current_ids)
        await self.tickets.unlink_tags(org_id, [ticket_id], current_ids - wanted_ids)
        await self.tickets.link_tags(org_id, [ticket_id], wanted_ids - current_ids)
        return [sorted(t.name for t in current), sorted(t.name for t in wanted)]
