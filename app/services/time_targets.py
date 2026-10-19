"""Daily and weekly time targets, working days and missing days (docs/modules/time.md).

A person's **work pattern** is their weekly capacity (``weekly_capacity_minutes``) spread over
their working weekdays (``working_days``, ISO 1 = Monday): every working weekday gets
``capacity // n`` minutes and the first ``capacity % n`` of them one minute more, so the daily
targets add up to the capacity exactly. A day before the person's start date or after their
leaving date is not a working day and has no target. Someone who is not a member of the
practice falls back to the practice's ``minutes_per_day`` and ``working_days``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from app.db.models import Organization, OrganizationMember


@dataclass(frozen=True, slots=True)
class WorkPattern:
    weekly_minutes: int
    weekdays: tuple[int, ...]
    start_date: date | None = None
    leaving_date: date | None = None

    @property
    def expects_time(self) -> bool:
        """False for someone with no capacity: they are never asked for a timesheet."""
        return self.weekly_minutes > 0 and bool(self.weekdays)

    def employed_on(self, day: date) -> bool:
        if self.start_date is not None and day < self.start_date:
            return False
        return self.leaving_date is None or day <= self.leaving_date

    def is_working_day(self, day: date) -> bool:
        return day.isoweekday() in self.weekdays and self.employed_on(day)

    def target_on(self, day: date) -> int:
        """The minutes expected on ``day``: 0 on a day off and outside the employment period."""
        if not self.is_working_day(day) or self.weekly_minutes <= 0:
            return 0
        base, extra = divmod(self.weekly_minutes, len(self.weekdays))
        return base + (1 if self.weekdays.index(day.isoweekday()) < extra else 0)


@dataclass(frozen=True, slots=True)
class WeekFigures:
    """What a week looks like against the person's pattern; every list follows ``days``."""

    daily_targets: dict[date, int]
    weekly_target: int
    working_days: list[date]
    missing_days: list[date]
    day_variance: list[int]


def pattern_of(organization: Organization, member: OrganizationMember | None) -> WorkPattern:
    if member is None:
        weekdays = tuple(sorted(set(organization.working_days)))
        return WorkPattern(organization.minutes_per_day * len(weekdays), weekdays)
    return WorkPattern(
        member.weekly_capacity_minutes,
        tuple(sorted(set(member.working_days))),
        member.start_date,
        member.leaving_date,
    )


def missing_days_of(
    pattern: WorkPattern, days: Sequence[date], logged: Mapping[date, int], today: date
) -> list[date]:
    """Working days up to and including ``today`` with no time logged."""
    if not pattern.expects_time:
        return []
    return [d for d in days if d <= today and pattern.is_working_day(d) and not logged.get(d)]


def week_figures(
    pattern: WorkPattern, days: Sequence[date], logged: Mapping[date, int], today: date
) -> WeekFigures:
    targets = {d: pattern.target_on(d) for d in days}
    return WeekFigures(
        daily_targets=targets,
        weekly_target=sum(targets.values()),
        working_days=[d for d in days if pattern.is_working_day(d)],
        missing_days=missing_days_of(pattern, days, logged, today),
        day_variance=[logged.get(d, 0) - targets[d] for d in days],
    )
