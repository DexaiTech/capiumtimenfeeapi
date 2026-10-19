"""Missing timesheets for the dashboard: the caller's scope, the week list and the count over
the weeks of a period.

The week list is :func:`app.services.timesheet_missing.missing_timesheets` itself. The count over
several weeks applies the same rule to many weeks with a fixed number of queries (a card for the
year would otherwise run the week query fifty times); ``tests/test_dashboard_pages_widgets.py``
checks that the two agree.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Organization
from app.repositories.dashboard_pages import DashboardPagesRepository
from app.repositories.time_overview import TimeOverviewRepository
from app.repositories.time_refs import TimeReferenceRepository
from app.schemas.dashboard_pages import MissingScopeName
from app.services.dashboard_common import sees_missing_timesheets
from app.services.policy import OrgContext, has_permission
from app.services.time_rules import practice_today, week_start_for
from app.services.time_targets import pattern_of, week_figures
from app.services.timesheet_missing import SUBMITTED_STATUSES, MissingScope

MAX_WEEKS = 60


@dataclass(frozen=True, slots=True)
class MissingAccess:
    scope: MissingScopeName
    # Whose timesheets the caller looks at; None is everyone's.
    user_ids: frozenset[uuid.UUID] | None

    def narrow(self) -> MissingScope:
        return MissingScope(user_ids=self.user_ids)


async def missing_access(session: AsyncSession, ctx: OrgContext) -> MissingAccess | None:
    """Everyone's with ``time.read_all``; with only ``timesheet.approve`` the people the caller
    manages and the caller; ``None`` for everybody else."""
    if not sees_missing_timesheets(ctx):
        return None
    if has_permission(ctx, "time.read_all"):
        return MissingAccess("practice", None)
    managed = await TimeReferenceRepository(session).managed_user_ids(
        ctx.organization_id, ctx.user.id
    )
    return MissingAccess("team", frozenset({*managed, ctx.user.id}))


def weeks_of_window(organization: Organization, date_from: date, today: date) -> list[date]:
    """The completed weeks that overlap ``[date_from, today]``, oldest first, and always the
    last completed week (the one that is due now)."""
    last = week_start_for(organization, today) - timedelta(days=7)
    weeks = [last]
    week = last - timedelta(days=7)
    while week + timedelta(days=6) >= date_from and len(weeks) < MAX_WEEKS:
        weeks.append(week)
        week -= timedelta(days=7)
    return sorted(weeks)


async def missing_person_weeks(
    session: AsyncSession,
    organization: Organization,
    weeks: Sequence[date],
    user_ids: frozenset[uuid.UUID] | None,
) -> list[tuple[date, uuid.UUID]]:
    """``(week_start, person)`` for every week in which an expected person has no submitted
    timesheet: the rule of ``missing_timesheets``, for several weeks at once."""
    if not weeks:
        return []
    overview = TimeOverviewRepository(session)
    first, last = min(weeks), max(weeks) + timedelta(days=6)
    staff = await overview.staff(
        organization.id, week_start=first, week_end=last, user_ids=user_ids, manager_user_id=None
    )
    if not staff:
        return []
    people = [member.user_id for member, _ in staff]
    sheets = await DashboardPagesRepository(session).sheet_statuses(organization.id, people, weeks)
    minutes = await overview.minutes_by_day(organization.id, people, first, last)
    today = practice_today(organization)
    found: list[tuple[date, uuid.UUID]] = []
    for week in weeks:
        days = [week + timedelta(days=i) for i in range(7)]
        for member, _ in staff:
            started = member.start_date is None or member.start_date <= days[-1]
            still_here = member.leaving_date is None or member.leaving_date >= week
            if not (started and still_here):
                continue
            if sheets.get((member.user_id, week)) in SUBMITTED_STATUSES:
                continue
            figures = week_figures(
                pattern_of(organization, member), days, minutes.get(member.user_id, {}), today
            )
            if figures.weekly_target > 0:
                found.append((week, member.user_id))
    return found
