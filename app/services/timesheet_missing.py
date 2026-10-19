"""Missing timesheets: who has not submitted a timesheet for a week (docs/modules/time.md).

One function, :func:`missing_timesheets`, serves the ``GET /timesheets/missing`` endpoint, the
reminders and the Dashboard / Missing Timesheets report, so they always agree.
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.db.models import Organization, Timesheet
from app.repositories.time_overview import TimeOverviewRepository
from app.repositories.timesheet_reminders import TimesheetReminderRepository
from app.schemas.time_views import MissingStatus, MissingTimesheet
from app.services.time_rules import practice_today, week_start_for
from app.services.time_targets import pattern_of, week_figures

SUBMITTED_STATUSES = ("submitted", "approved")


@dataclass(frozen=True, slots=True)
class MissingScope:
    """Whose timesheets to look at: these people (``None``: everyone) and, when set, only the
    people ``manager_user_id`` approves."""

    user_ids: frozenset[uuid.UUID] | None = None
    manager_user_id: uuid.UUID | None = None


def last_completed_week(organization: Organization, today: date | None = None) -> date:
    """The ``week_start`` of the week before the current one in the practice."""
    current = week_start_for(organization, today or practice_today(organization))
    return current - timedelta(days=7)


async def load_practice(session: AsyncSession, organization_id: uuid.UUID) -> Organization:
    organization = await session.get(Organization, organization_id)
    if organization is None or organization.deleted_at is not None:
        raise NotFoundError("Organization not found.")
    return organization


async def missing_timesheets(
    session: AsyncSession,
    organization_id: uuid.UUID,
    week_start: date,
    scope: MissingScope | None = None,
    *,
    organization: Organization | None = None,
) -> list[MissingTimesheet]:
    """Staff whose timesheet for the week of ``week_start`` (any day of it) is not submitted.

    Who is expected to record time: active staff with a weekly capacity above 0 who are
    employed on at least one working day of the week (start and leaving dates, working days).
    Their ``status`` is ``none`` (no timesheet for the week), ``open`` (not submitted) or
    ``rejected`` (returned); people with a ``submitted`` or ``approved`` sheet are not listed.
    ``expected_minutes`` is the person's target for the week, ``missing_days`` their working
    days up to today with no time. Sorted by name.
    """
    org = organization or await load_practice(session, organization_id)
    scope = scope or MissingScope()
    start = week_start_for(org, week_start)
    days = [start + timedelta(days=i) for i in range(7)]
    overview = TimeOverviewRepository(session)
    staff = await overview.staff(
        org.id,
        week_start=start,
        week_end=days[-1],
        user_ids=scope.user_ids,
        manager_user_id=scope.manager_user_id,
    )
    if not staff:
        return []
    people = [member.user_id for member, _ in staff]
    sheets = await overview.sheets_of_week(org.id, start, people)
    minutes = await overview.minutes_by_day(org.id, people, days[0], days[-1])
    reminded = await TimesheetReminderRepository(session).last_reminded(
        org.id, start, "timesheet", people
    )
    today = practice_today(org)

    rows: list[MissingTimesheet] = []
    for member, name in staff:
        sheet = sheets.get(member.user_id)
        if sheet is not None and sheet.status in SUBMITTED_STATUSES:
            continue
        logged = minutes.get(member.user_id, {})
        figures = week_figures(pattern_of(org, member), days, logged, today)
        if figures.weekly_target <= 0:
            continue
        rows.append(
            MissingTimesheet(
                user_id=member.user_id,
                name=name,
                week_start=start,
                status=_status_of(sheet),
                timesheet_id=sheet.id if sheet else None,
                logged_minutes=sum(logged.values()),
                expected_minutes=figures.weekly_target,
                missing_days=figures.missing_days,
                last_reminded_at=reminded.get(member.user_id),
                has_login=member.login_enabled,
            )
        )
    return rows


def _status_of(sheet: Timesheet | None) -> MissingStatus:
    if sheet is None:
        return "none"
    return "rejected" if sheet.status == "rejected" else "open"
