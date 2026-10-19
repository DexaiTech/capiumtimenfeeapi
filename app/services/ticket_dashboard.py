"""The Application Testing dashboard (docs/modules/tickets.md, "Dashboard").

Every figure is about the tickets *created* in the date range, days being the practice's
calendar days. The previous period is the same number of days immediately before the range.
The status trend is rebuilt from the status history: the status of a ticket at the end of a day
is the last one written before that moment.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.db.models.tickets import TICKET_PRIORITIES, TICKET_STATUSES
from app.repositories.ticket_dashboard import AssigneeUsage, TicketDashboardRepository
from app.schemas.ticket_dashboard import Change, TicketDashboard
from app.services.policy import OrgContext
from app.services.ticket_reader import TicketReader
from app.utils.money import round_half_up
from app.utils.practice_dates import end_of_day_utc, start_of_day_utc, today_in

DEFAULT_DAYS = 19
MAX_DAYS = 366
TREND_DAYS = 14
TOP_TAGS = 7
TOP_ASSIGNEES = 10
RECENT_TICKETS = 7


def resolve_range(date_from: date | None, date_to: date | None, today: date) -> tuple[date, date]:
    """The range asked for; by default the last ``DEFAULT_DAYS`` days ending today."""
    end = date_to or today
    start = date_from or end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        raise ValidationError.for_field("date_from", "The start cannot be after the end.")
    if (end - start).days + 1 > MAX_DAYS:
        raise ValidationError.for_field("date_to", f"The range is at most {MAX_DAYS} days.")
    return start, end


def change_of(current: int, previous: int) -> Change:
    """``New`` when there was nothing before, else the whole percent change (half away from 0)."""
    if previous == 0:
        return Change(kind="new" if current > 0 else "none", percent=None)
    return Change(
        kind="percent",
        percent=round_half_up(Decimal(current - previous) * 100 / Decimal(previous)),
    )


def share_of(count: int, total: int) -> float | None:
    """``count`` as a percentage of ``total`` to one decimal (half up); null without a total."""
    if total == 0:
        return None
    return round_half_up(Decimal(count) * 1000 / Decimal(total)) / 10


def _kpi(current: int, previous: int) -> dict[str, Any]:
    return {"value": current, "previous": previous, "change": change_of(current, previous)}


def _assignee_rows(usage: list[AssigneeUsage]) -> list[dict[str, Any]]:
    """The ``TOP_ASSIGNEES`` busiest people, plus the unassigned tickets as their own row."""
    named = [u for u in usage if u.user_id is not None][:TOP_ASSIGNEES]
    unassigned = [u for u in usage if u.user_id is None]
    rows: list[dict[str, Any]] = [
        {"user_id": u.user_id, "full_name": u.full_name or "", "count": u.count} for u in named
    ] + [{"user_id": None, "full_name": "Unassigned", "count": u.count} for u in unassigned]
    rows.sort(key=lambda r: (-r["count"], r["full_name"].lower()))
    return rows


class TicketDashboardService:
    def __init__(self, session: AsyncSession) -> None:
        self.repo = TicketDashboardRepository(session)
        self.reader = TicketReader(session)

    async def dashboard(
        self, ctx: OrgContext, date_from: date | None, date_to: date | None
    ) -> TicketDashboard:
        org_id: uuid.UUID = ctx.organization_id
        zone = ctx.organization.timezone
        start, end = resolve_range(date_from, date_to, today_in(zone))
        days = (end - start).days + 1
        previous_start = start - timedelta(days=days)
        start_at, end_at = start_of_day_utc(start, zone), end_of_day_utc(end, zone)

        current, previous = await self.repo.status_counts(
            org_id, start_at, end_at, start_of_day_utc(previous_start, zone)
        )
        total, previous_total = sum(current.values()), sum(previous.values())
        priorities = await self.repo.priority_counts(org_id, start_at, end_at)
        tag_usage = await self.repo.tag_usage(org_id, start_at, end_at)
        assignees = await self.repo.assignee_usage(org_id, start_at, end_at)
        recent = await self.reader.items(
            org_id, await self.repo.recent(org_id, start_at, end_at, RECENT_TICKETS)
        )

        trend_days = [end - timedelta(days=TREND_DAYS - 1 - i) for i in range(TREND_DAYS)]
        snapshots = await self.repo.trend(
            org_id, start_at, end_at, [(d, end_of_day_utc(d, zone)) for d in trend_days]
        )
        trend = []
        for day in trend_days:
            counts = {s: snapshots.get(day, {}).get(s, 0) for s in TICKET_STATUSES}
            trend.append({"date": day, **counts, "total": sum(counts.values())})

        by_tag: list[dict[str, Any]] = [
            {"tag_id": u.tag_id, "name": u.name, "color": u.color, "count": u.count}
            | {"is_other": False}
            for u in tag_usage[:TOP_TAGS]
        ]
        if len(tag_usage) > TOP_TAGS:
            others = await self.repo.tickets_with_other_tags(
                org_id, start_at, end_at, [u.tag_id for u in tag_usage[:TOP_TAGS]]
            )
            by_tag.append(
                {"tag_id": None, "name": "Others", "color": None, "count": others, "is_other": True}
            )

        return TicketDashboard.model_validate(
            {
                "date_from": start,
                "date_to": end,
                "previous_from": previous_start,
                "previous_to": start - timedelta(days=1),
                "days": days,
                "kpis": {
                    "total": _kpi(total, previous_total),
                    **{s: _kpi(current.get(s, 0), previous.get(s, 0)) for s in TICKET_STATUSES},
                },
                "by_status": [
                    {
                        "status": s,
                        "count": current.get(s, 0),
                        "percent": share_of(current.get(s, 0), total),
                    }
                    for s in TICKET_STATUSES
                ],
                "by_priority": [
                    {
                        "priority": p,
                        "count": priorities.get(p, 0),
                        "percent": share_of(priorities.get(p, 0), total),
                    }
                    for p in TICKET_PRIORITIES
                ],
                "by_tag": by_tag,
                "trend": trend,
                "top_assignees": _assignee_rows(assignees),
                "recent": recent,
            }
        )
