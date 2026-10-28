"""The days on which a client is reminded about an unpaid invoice.

Each entry counts days from the invoice's due date: negative before it, 0 on it, positive after.
A practice that never set a schedule has the one reminder of ``payment_reminder_days_after_due``.
"""

from collections.abc import Iterable
from datetime import date, timedelta

MAX_STEPS = 12
EARLIEST = -90
LATEST = 365


def effective_schedule(stored: Iterable[int], days_after_due: int) -> list[int]:
    """The sorted, distinct reminder days: the stored schedule, else one reminder after the due
    date."""
    days = sorted(set(stored))
    return days or [days_after_due]


def latest_step_reached(schedule: Iterable[int], due_date: date, today: date) -> date | None:
    """The date of the most recent reminder day that has come (``due_date`` plus the largest
    entry that is not in the future), or None while none has."""
    reached = [due_date + timedelta(days=day) for day in schedule]
    passed = [d for d in reached if d <= today]
    return max(passed) if passed else None
