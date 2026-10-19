"""Queries behind the Application Testing dashboard. Every method is scoped to one organization.

All figures are about the tickets *created* in a range of instants (the caller turns the
practice's calendar days into UTC bounds). The status trend is rebuilt from the status history:
a ticket is in the status of the last history row written before the end of the day.
"""

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Date, DateTime, and_, column, func, or_, select, values
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Ticket, TicketStatusHistory, TicketTag, TicketTagLink, User
from app.repositories.tickets import TicketRow, ticket_rows_select


@dataclass(frozen=True, slots=True)
class TagUsage:
    tag_id: uuid.UUID
    name: str
    color: str
    count: int


@dataclass(frozen=True, slots=True)
class AssigneeUsage:
    user_id: uuid.UUID | None
    full_name: str | None
    count: int


class TicketDashboardRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def status_counts(
        self, organization_id: uuid.UUID, start: datetime, end: datetime, previous_start: datetime
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Tickets by current status created in ``[start, end)`` and in ``[previous_start,
        start)``, in one pass."""
        in_range = and_(Ticket.created_at >= start, Ticket.created_at < end)
        in_previous = and_(Ticket.created_at >= previous_start, Ticket.created_at < start)
        stmt = (
            select(
                Ticket.status,
                func.count().filter(in_range).label("current"),
                func.count().filter(in_previous).label("previous"),
            )
            .where(
                Ticket.organization_id == organization_id,
                Ticket.created_at >= previous_start,
                Ticket.created_at < end,
            )
            .group_by(Ticket.status)
        )
        current: dict[str, int] = {}
        previous: dict[str, int] = {}
        for status, now_count, before_count in (await self.session.execute(stmt)).all():
            current[status] = now_count
            previous[status] = before_count
        return current, previous

    async def priority_counts(
        self, organization_id: uuid.UUID, start: datetime, end: datetime
    ) -> dict[str, int]:
        stmt = (
            select(Ticket.priority, func.count())
            .where(
                Ticket.organization_id == organization_id,
                Ticket.created_at >= start,
                Ticket.created_at < end,
            )
            .group_by(Ticket.priority)
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

    async def tag_usage(
        self, organization_id: uuid.UUID, start: datetime, end: datetime
    ) -> list[TagUsage]:
        """Every tag carried by a ticket of the range, most used first (ties by name)."""
        used = func.count().label("used")
        stmt = (
            select(TicketTag.id, TicketTag.name, TicketTag.color, used)
            .select_from(TicketTagLink)
            .join(
                Ticket,
                and_(
                    Ticket.organization_id == TicketTagLink.organization_id,
                    Ticket.id == TicketTagLink.ticket_id,
                ),
            )
            .join(
                TicketTag,
                and_(
                    TicketTag.organization_id == TicketTagLink.organization_id,
                    TicketTag.id == TicketTagLink.tag_id,
                ),
            )
            .where(
                Ticket.organization_id == organization_id,
                Ticket.created_at >= start,
                Ticket.created_at < end,
            )
            .group_by(TicketTag.id, TicketTag.name, TicketTag.color)
            .order_by(used.desc(), func.lower(TicketTag.name), TicketTag.id)
        )
        return [TagUsage(*row) for row in (await self.session.execute(stmt)).all()]

    async def tickets_with_other_tags(
        self,
        organization_id: uuid.UUID,
        start: datetime,
        end: datetime,
        listed_tag_ids: Collection[uuid.UUID],
    ) -> int:
        """Tickets of the range that carry at least one tag that is not in ``listed_tag_ids``;
        a ticket counts once however many such tags it has."""
        stmt = (
            select(func.count(func.distinct(TicketTagLink.ticket_id)))
            .select_from(TicketTagLink)
            .join(
                Ticket,
                and_(
                    Ticket.organization_id == TicketTagLink.organization_id,
                    Ticket.id == TicketTagLink.ticket_id,
                ),
            )
            .where(
                Ticket.organization_id == organization_id,
                Ticket.created_at >= start,
                Ticket.created_at < end,
                TicketTagLink.tag_id.not_in(listed_tag_ids),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def assignee_usage(
        self, organization_id: uuid.UUID, start: datetime, end: datetime
    ) -> list[AssigneeUsage]:
        """Tickets per assignee, with the unassigned ones as a row without a user."""
        used = func.count().label("used")
        stmt = (
            select(Ticket.assignee_user_id, User.full_name, used)
            .select_from(Ticket)
            .outerjoin(User, User.id == Ticket.assignee_user_id)
            .where(
                Ticket.organization_id == organization_id,
                Ticket.created_at >= start,
                Ticket.created_at < end,
            )
            .group_by(Ticket.assignee_user_id, User.full_name)
            .order_by(used.desc(), func.lower(User.full_name).nulls_last(), Ticket.assignee_user_id)
        )
        return [AssigneeUsage(*row) for row in (await self.session.execute(stmt)).all()]

    async def recent(
        self, organization_id: uuid.UUID, start: datetime, end: datetime, limit: int
    ) -> list[TicketRow]:
        stmt = (
            ticket_rows_select(with_description=False)
            .where(
                Ticket.organization_id == organization_id,
                Ticket.created_at >= start,
                Ticket.created_at < end,
            )
            .order_by(Ticket.created_at.desc(), Ticket.id.desc())
            .limit(limit)
        )
        return [TicketRow(r[0], r[1], r[2], r[3]) for r in (await self.session.execute(stmt)).all()]

    async def trend(
        self,
        organization_id: uuid.UUID,
        start: datetime,
        end: datetime,
        days: list[tuple[date, datetime]],
    ) -> dict[date, dict[str, int]]:
        """Per day, the tickets of the range by their status at the end of that day.

        ``days`` pairs each calendar day with the instant it ends (exclusive). Each history row
        is valid from its ``changed_at`` until the ticket's next row, so a ticket counts on the
        days whose end falls inside that span - and not before it was created.
        """
        history = TicketStatusHistory
        until = (
            func.lead(history.changed_at)
            .over(partition_by=history.ticket_id, order_by=(history.changed_at, history.id))
            .label("until_at")
        )
        spans = (
            select(history.to_status.label("status"), history.changed_at.label("from_at"), until)
            .join(
                Ticket,
                and_(
                    Ticket.organization_id == history.organization_id,
                    Ticket.id == history.ticket_id,
                ),
            )
            .where(
                history.organization_id == organization_id,
                Ticket.created_at >= start,
                Ticket.created_at < end,
            )
            .cte("spans")
        )
        wanted = values(
            column("day", Date), column("ends_at", DateTime(timezone=True)), name="wanted_days"
        ).data(days)
        stmt = (
            select(wanted.c.day, spans.c.status, func.count())
            .select_from(wanted)
            .join(
                spans,
                and_(
                    spans.c.from_at < wanted.c.ends_at,
                    or_(spans.c.until_at.is_(None), spans.c.until_at >= wanted.c.ends_at),
                ),
            )
            .group_by(wanted.c.day, spans.c.status)
        )
        found: dict[date, dict[str, int]] = {}
        for day, status, count in (await self.session.execute(stmt)).all():
            found.setdefault(day, {})[status] = count
        return found
