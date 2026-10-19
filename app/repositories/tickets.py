"""Ticket queries. Every method is scoped to one organization.

The ticket list is one keyset-paginated query (counts of comments and files are scalar
subqueries of it); what a page needs besides that - the tags and the names of the people - is
fetched with one batched query each, never per row.
"""

import base64
import json
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Select,
    and_,
    case,
    delete,
    exists,
    func,
    or_,
    select,
    tuple_,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.core.exceptions import ValidationError
from app.db.models import (
    OrganizationMember,
    Ticket,
    TicketAttachment,
    TicketComment,
    TicketStatusHistory,
    TicketTag,
    TicketTagLink,
    User,
)
from app.db.models.tickets import EXCERPT_LENGTH, TICKET_STATUSES

_PRIORITY_RANK = {"high": 1, "medium": 2, "low": 3}

type AnySelect = Select[*tuple[Any, ...]]


@dataclass(frozen=True, slots=True)
class TicketFilters:
    q: str | None = None
    statuses: Sequence[str] = ()
    priorities: Sequence[str] = ()
    tag_ids: Sequence[uuid.UUID] = ()
    assignee_id: uuid.UUID | None = None
    unassigned: bool = False
    # UTC instants: created at or after ``created_from`` and before ``created_to``.
    created_from: datetime | None = None
    created_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class TicketRow:
    ticket: Ticket
    excerpt: str | None
    comment_count: int
    attachment_count: int


@dataclass(frozen=True, slots=True)
class TicketPage:
    rows: list[TicketRow]
    next_cursor: str | None


# --- Keyset cursor carrying the sort ---------------------------------------------------


def normalise_sort(sort: str) -> str:
    return "number" if sort == "code" else sort


def sort_key(sort: str) -> ColumnElement[Any]:
    match sort:
        case "number":
            return Ticket.number.expression
        case "subject":
            return func.lower(Ticket.subject)
        case "status":
            return Ticket.status.expression
        case "priority":
            return case(_PRIORITY_RANK, value=Ticket.priority, else_=4)
        case _:
            return Ticket.created_at.expression


def encode_cursor(sort: str, descending: bool, value: Any, row_id: uuid.UUID) -> str:
    is_time = isinstance(value, datetime)
    payload = {
        "s": sort,
        "d": descending,
        "k": "dt" if is_time else "raw",
        "v": value.isoformat() if is_time else value,
        "id": str(row_id),
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_value(sort: str, kind: Any, value: Any) -> Any:
    """The sort key of a cursor, checked against the type of the sort's SQL key: a tampered
    cursor answers 422 and never reaches the database as a mistyped parameter."""
    if sort == "created_at":
        if kind != "dt" or not isinstance(value, str):
            raise ValueError("expected a timestamp")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("expected an aware timestamp")
        return parsed
    if kind != "raw":
        raise ValueError("bad kind")
    if sort == "number":
        if type(value) is not int or not 1 <= value <= 2_147_483_647:
            raise ValueError("expected a ticket number")
        return value
    if sort == "priority":
        if type(value) is not int or not 1 <= value <= 4:
            raise ValueError("expected a priority rank")
        return value
    if sort == "status":
        if value not in TICKET_STATUSES:
            raise ValueError("expected a status")
        return value
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("expected text")
    return value


def decode_cursor(cursor: str, sort: str, descending: bool) -> tuple[Any, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if data["s"] != sort or bool(data["d"]) != descending:
            raise ValueError("cursor of another sort")
        if not isinstance(data["id"], str):
            raise ValueError("bad id")
        return _cursor_value(sort, data["k"], data["v"]), uuid.UUID(data["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationError.for_field(
            "cursor", "Invalid cursor (the sort or filters may have changed)."
        ) from exc


def _counted(model: Any) -> ColumnElement[int]:
    return (
        select(func.count())
        .select_from(model)
        .where(model.organization_id == Ticket.organization_id, model.ticket_id == Ticket.id)
        .correlate(Ticket)
        .scalar_subquery()
    )


def ticket_rows_select(*, with_description: bool) -> AnySelect:
    """Tickets with their description excerpt and counts of comments and files."""
    stmt = select(
        Ticket,
        func.left(Ticket.description, EXCERPT_LENGTH).label("excerpt"),
        _counted(TicketComment).label("comment_count"),
        _counted(TicketAttachment).label("attachment_count"),
    )
    return stmt if with_description else stmt.options(defer(Ticket.description))


def history_entry(
    organization_id: uuid.UUID,
    ticket_id: uuid.UUID,
    *,
    from_status: str | None,
    to_status: str,
    changed_by: uuid.UUID,
    changed_at: datetime,
) -> TicketStatusHistory:
    """One row of the status history; ``from_status`` is None for a ticket's first status."""
    return TicketStatusHistory(
        organization_id=organization_id,
        ticket_id=ticket_id,
        from_status=from_status,
        to_status=to_status,
        changed_by=changed_by,
        changed_at=changed_at,
    )


class TicketRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, ticket: Ticket) -> Ticket:
        self.session.add(ticket)
        await self.session.flush()
        return ticket

    async def get(
        self, organization_id: uuid.UUID, ticket_id: uuid.UUID, *, lock: bool = False
    ) -> Ticket | None:
        stmt = (
            select(Ticket)
            .where(Ticket.organization_id == organization_id, Ticket.id == ticket_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def code_of(self, organization_id: uuid.UUID, ticket_id: uuid.UUID) -> str | None:
        """The ticket's code, or None when the practice has no such ticket."""
        stmt = select(Ticket.code).where(
            Ticket.organization_id == organization_id, Ticket.id == ticket_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_many(
        self, organization_id: uuid.UUID, ticket_ids: Collection[uuid.UUID]
    ) -> list[Ticket]:
        """The practice's tickets among ``ticket_ids``, locked in id order (no deadlocks)."""
        stmt = (
            select(Ticket)
            .where(Ticket.organization_id == organization_id, Ticket.id.in_(ticket_ids))
            .order_by(Ticket.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get_row(self, organization_id: uuid.UUID, ticket_id: uuid.UUID) -> TicketRow | None:
        """One ticket with its full description (a detail read)."""
        stmt = ticket_rows_select(with_description=True).where(
            Ticket.organization_id == organization_id, Ticket.id == ticket_id
        )
        stmt = stmt.execution_options(populate_existing=True)
        row = (await self.session.execute(stmt)).one_or_none()
        return None if row is None else TicketRow(row[0], row[1], row[2], row[3])

    # --- The list ---------------------------------------------------------------------

    @staticmethod
    def filtered(stmt: AnySelect, organization_id: uuid.UUID, filters: TicketFilters) -> AnySelect:
        stmt = stmt.where(Ticket.organization_id == organization_id)
        if filters.q:
            stmt = stmt.where(
                or_(
                    Ticket.code.icontains(filters.q, autoescape=True),
                    Ticket.subject.icontains(filters.q, autoescape=True),
                    Ticket.description.icontains(filters.q, autoescape=True),
                )
            )
        if filters.statuses:
            stmt = stmt.where(Ticket.status.in_(filters.statuses))
        if filters.priorities:
            stmt = stmt.where(Ticket.priority.in_(filters.priorities))
        if filters.tag_ids:
            stmt = stmt.where(
                exists().where(
                    TicketTagLink.organization_id == Ticket.organization_id,
                    TicketTagLink.ticket_id == Ticket.id,
                    TicketTagLink.tag_id.in_(filters.tag_ids),
                )
            )
        if filters.assignee_id is not None:
            stmt = stmt.where(Ticket.assignee_user_id == filters.assignee_id)
        if filters.unassigned:
            stmt = stmt.where(Ticket.assignee_user_id.is_(None))
        if filters.created_from is not None:
            stmt = stmt.where(Ticket.created_at >= filters.created_from)
        if filters.created_to is not None:
            stmt = stmt.where(Ticket.created_at < filters.created_to)
        return stmt

    async def list_page(
        self,
        organization_id: uuid.UUID,
        filters: TicketFilters,
        *,
        sort: str,
        descending: bool,
        limit: int,
        cursor: str | None,
    ) -> TicketPage:
        """One page ordered by ``(sort key, id)``; the cursor carries the sort key."""
        sort = normalise_sort(sort)
        key = sort_key(sort)
        stmt = self.filtered(
            ticket_rows_select(with_description=False).add_columns(key.label("sort_key")),
            organization_id,
            filters,
        )
        if cursor:
            value, row_id = decode_cursor(cursor, sort, descending)
            pair = tuple_(key, Ticket.id)
            stmt = stmt.where(pair < (value, row_id) if descending else pair > (value, row_id))
        order = (key.desc(), Ticket.id.desc()) if descending else (key.asc(), Ticket.id.asc())
        found = (await self.session.execute(stmt.order_by(*order).limit(limit + 1))).all()
        next_cursor = None
        if len(found) > limit:
            found = found[:limit]
            last = found[-1]
            next_cursor = encode_cursor(sort, descending, last.sort_key, last[0].id)
        return TicketPage(
            rows=[TicketRow(r[0], r[1], r[2], r[3]) for r in found], next_cursor=next_cursor
        )

    async def tags_for(
        self, organization_id: uuid.UUID, ticket_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, list[TicketTag]]:
        if not ticket_ids:
            return {}
        stmt = (
            select(TicketTagLink.ticket_id, TicketTag)
            .join(
                TicketTag,
                and_(
                    TicketTag.organization_id == TicketTagLink.organization_id,
                    TicketTag.id == TicketTagLink.tag_id,
                ),
            )
            .where(
                TicketTagLink.organization_id == organization_id,
                TicketTagLink.ticket_id.in_(ticket_ids),
            )
            .order_by(func.lower(TicketTag.name), TicketTag.id)
        )
        found: dict[uuid.UUID, list[TicketTag]] = {}
        for ticket_id, tag in (await self.session.execute(stmt)).all():
            found.setdefault(ticket_id, []).append(tag)
        return found

    async def names(
        self, organization_id: uuid.UUID, user_ids: Collection[uuid.UUID | None]
    ) -> dict[uuid.UUID, str]:
        wanted = {i for i in user_ids if i is not None}
        if not wanted:
            return {}
        stmt = (
            select(OrganizationMember.user_id, User.full_name)
            .join(User, User.id == OrganizationMember.user_id)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id.in_(wanted),
            )
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

    async def is_active_member(self, organization_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        stmt = select(OrganizationMember.user_id).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
            OrganizationMember.status == "active",
        )
        return (await self.session.execute(stmt.limit(1))).first() is not None

    # --- Tags of a ticket -------------------------------------------------------------

    async def tag_ids_of(self, organization_id: uuid.UUID, ticket_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = select(TicketTagLink.tag_id).where(
            TicketTagLink.organization_id == organization_id,
            TicketTagLink.ticket_id == ticket_id,
        )
        return list((await self.session.execute(stmt)).scalars())

    async def ticket_ids_with_tag(
        self,
        organization_id: uuid.UUID,
        ticket_ids: Collection[uuid.UUID],
        tag_id: uuid.UUID,
    ) -> set[uuid.UUID]:
        """Those of ``ticket_ids`` that carry the tag."""
        stmt = select(TicketTagLink.ticket_id).where(
            TicketTagLink.organization_id == organization_id,
            TicketTagLink.tag_id == tag_id,
            TicketTagLink.ticket_id.in_(ticket_ids),
        )
        return set((await self.session.execute(stmt)).scalars())

    async def link_tags(
        self,
        organization_id: uuid.UUID,
        ticket_ids: Collection[uuid.UUID],
        tag_ids: Collection[uuid.UUID],
    ) -> None:
        """Every ticket gets every tag; links that exist stay as they are."""
        rows = [
            {"organization_id": organization_id, "ticket_id": ticket_id, "tag_id": tag_id}
            for ticket_id in ticket_ids
            for tag_id in tag_ids
        ]
        if rows:
            await self.session.execute(insert(TicketTagLink).values(rows).on_conflict_do_nothing())

    async def unlink_tags(
        self,
        organization_id: uuid.UUID,
        ticket_ids: Collection[uuid.UUID],
        tag_ids: Collection[uuid.UUID],
    ) -> None:
        if ticket_ids and tag_ids:
            await self.session.execute(
                delete(TicketTagLink)
                .where(
                    TicketTagLink.organization_id == organization_id,
                    TicketTagLink.ticket_id.in_(ticket_ids),
                    TicketTagLink.tag_id.in_(tag_ids),
                )
                .execution_options(synchronize_session=False)
            )

    # --- History and deletion ---------------------------------------------------------

    async def add_history(self, rows: Sequence[TicketStatusHistory]) -> None:
        """Record status changes (and the first status of new tickets) with one flush."""
        self.session.add_all(rows)
        await self.session.flush()

    async def attachment_keys(
        self, organization_id: uuid.UUID, ticket_ids: Collection[uuid.UUID]
    ) -> list[str]:
        stmt = select(TicketAttachment.storage_key).where(
            TicketAttachment.organization_id == organization_id,
            TicketAttachment.ticket_id.in_(ticket_ids),
        )
        return list((await self.session.execute(stmt)).scalars())

    async def delete_many(
        self, organization_id: uuid.UUID, ticket_ids: Collection[uuid.UUID]
    ) -> None:
        """Delete the tickets; their comments, files, tag links and history go with them."""
        await self.session.execute(
            delete(Ticket)
            .where(Ticket.organization_id == organization_id, Ticket.id.in_(ticket_ids))
            .execution_options(synchronize_session=False)
        )
        await self.session.flush()


__all__ = [
    "AnySelect",
    "TicketFilters",
    "TicketPage",
    "TicketRepository",
    "TicketRow",
    "decode_cursor",
    "encode_cursor",
    "history_entry",
    "normalise_sort",
    "sort_key",
    "ticket_rows_select",
]
