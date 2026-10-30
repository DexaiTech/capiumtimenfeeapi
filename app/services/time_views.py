"""The day and calendar views of a person's time (docs/modules/time.md)."""

import uuid
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.repositories.time_entries import TimerRepository
from app.repositories.time_overview import TimeOverviewRepository
from app.schemas.time import TimeEntryRead, TimerRead
from app.schemas.time_views import MAX_CALENDAR_DAYS, CalendarDay, TimeCalendar, TimeDay
from app.services import period_locks
from app.services.policy import OrgContext
from app.services.time_entries import TimeEntryService, show_cost
from app.services.time_rules import practice_date, week_start_for
from app.services.time_targets import pattern_of
from app.services.time_visibility import can_see_note
from app.services.timers import elapsed_seconds


class TimeViewService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.time = TimeEntryService(session)
        self.overview = TimeOverviewRepository(session)
        self.timers = TimerRepository(session)

    async def _person(self, ctx: OrgContext, user_id: str | None) -> uuid.UUID:
        """``me`` (default) or a user id the caller may view: their own, anyone's with
        ``time.read_all``, or the people they manage."""
        person = ctx.user.id
        if user_id not in (None, "me"):
            assert user_id is not None
            try:
                person = uuid.UUID(user_id)
            except ValueError as exc:
                raise ValidationError.for_field("user_id", "Use a user id or 'me'.") from exc
        await self.time.ensure_can_view_person(ctx, person)
        return person

    async def day(self, ctx: OrgContext, *, day: date, user_id: str | None) -> TimeDay:
        org = ctx.organization
        person = await self._person(ctx, user_id)
        member = await self.time.refs.get_member(org.id, person)
        pattern = pattern_of(org, member)
        views = await self.time.entries.range_views(org.id, person, day, day)
        start = week_start_for(org, day)
        sheet = await self.time.sheets.find(org.id, person, start)
        cost = show_cost(ctx)
        locked = await period_locks.locked_through(self.session, org.id, [day])
        total = sum(v.entry.minutes for v in views)
        billable = sum(v.entry.minutes for v in views if v.entry.is_billable)
        target = pattern.target_on(day)
        timer: TimerRead | None = None
        if person == ctx.user.id:
            running = await self.timers.get(org.id, person)
            if running is not None:
                timer = TimerRead.build(
                    running, elapsed_seconds(running), practice_date(org, running.created_at)
                )
        return TimeDay(
            user_id=person,
            date=day,
            week_start=start,
            timesheet_id=sheet.id if sheet else None,
            timesheet_status=sheet.status if sheet else "open",
            is_working_day=pattern.is_working_day(day),
            entries=[
                TimeEntryRead.from_view(
                    v,
                    show_cost=cost,
                    show_note=can_see_note(ctx, v.entry.user_id),
                    locked_through=locked.get(day),
                )
                for v in views
            ],
            total_minutes=total,
            billable_minutes=billable,
            non_billable_minutes=total - billable,
            target_minutes=target,
            remaining_minutes=max(target - total, 0),
            variance_minutes=total - target,
            timer=timer,
        )

    async def calendar(
        self, ctx: OrgContext, *, date_from: date, date_to: date, user_id: str | None
    ) -> TimeCalendar:
        """One row per day of ``[date_from, date_to]`` (at most 62 days)."""
        if date_from > date_to:
            raise ValidationError.for_field("to", "The range cannot end before it starts.")
        if (date_to - date_from).days + 1 > MAX_CALENDAR_DAYS:
            raise ValidationError.for_field(
                "to", f"The calendar covers at most {MAX_CALENDAR_DAYS} days.", "range_too_long"
            )
        org = ctx.organization
        person = await self._person(ctx, user_id)
        member = await self.time.refs.get_member(org.id, person)
        pattern = pattern_of(org, member)
        figures = await self.overview.day_figures(org.id, person, date_from, date_to)
        sheets = await self.overview.sheets_by_week(
            org.id, person, week_start_for(org, date_from), date_to
        )
        days: list[CalendarDay] = []
        for offset in range((date_to - date_from).days + 1):
            day = date_from + timedelta(days=offset)
            week = week_start_for(org, day)
            found = figures.get(day)
            sheet = sheets.get(week)
            days.append(
                CalendarDay(
                    date=day,
                    week_start=week,
                    entry_count=found.entry_count if found else 0,
                    total_minutes=found.total_minutes if found else 0,
                    billable_minutes=found.billable_minutes if found else 0,
                    target_minutes=pattern.target_on(day),
                    is_working_day=pattern.is_working_day(day),
                    timesheet_status=sheet.status if sheet else "open",
                )
            )
        return TimeCalendar(
            user_id=person,
            date_from=date_from,
            date_to=date_to,
            days=days,
            total_minutes=sum(d.total_minutes for d in days),
            billable_minutes=sum(d.billable_minutes for d in days),
            target_minutes=sum(d.target_minutes for d in days),
        )
