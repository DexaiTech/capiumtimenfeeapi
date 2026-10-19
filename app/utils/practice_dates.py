"""Calendar dates in a practice's time zone."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def today_in(timezone: str) -> date:
    return datetime.now(ZoneInfo(timezone)).date()


def start_of_day_utc(day: date, timezone: str) -> datetime:
    """The UTC instant at which ``day`` begins in the time zone."""
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(timezone)).astimezone(UTC)


def end_of_day_utc(day: date, timezone: str) -> datetime:
    """The UTC instant at which the day after ``day`` begins (an exclusive upper bound)."""
    return start_of_day_utc(day + timedelta(days=1), timezone)
