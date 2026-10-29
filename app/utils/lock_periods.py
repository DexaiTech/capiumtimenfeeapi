"""The periods a practice can lock: a week, a month, a quarter or a whole accounting year.

The accounting year starts on the practice's ``financial_year_start_month``/``_day`` (1 January:
the calendar year) and is named by the calendar year it starts in (``2026`` runs from 06/04/2026
to 05/04/2027 when the year starts on 6 April). Months and quarters are counted from its start;
a week is a full ``week_start`` to ``week_start + 6`` week and belongs to the year it begins in,
so a year has 52 or 53 of them and the first and last may run over into the next or previous year.
"""

from datetime import date, timedelta
from typing import Literal

from app.db.models.organization import LockGranularity

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# The kind of lock each way of locking creates.
GRANULARITY_PERIOD: dict[LockGranularity, Literal["week", "month", "quarter", "year"]] = {
    "weekly": "week",
    "monthly": "month",
    "quarterly": "quarter",
    "annually": "year",
}
PERIOD_GRANULARITY = {period: granularity for granularity, period in GRANULARITY_PERIOD.items()}
PERIODS_PER_YEAR = {"week": 53, "month": 12, "quarter": 4, "year": 1}


def _add_months(day: date, months: int) -> date:
    """``day`` moved by whole months; the day of the month is 1-28, so it always exists."""
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, day.day)


def year_bounds(year: int, start_month: int = 1, start_day: int = 1) -> tuple[date, date]:
    """First and last day of the accounting year named ``year``."""
    start = date(year, start_month, start_day)
    return start, date(year + 1, start_month, start_day) - timedelta(days=1)


def accounting_year_of(day: date, start_month: int = 1, start_day: int = 1) -> int:
    """The accounting year that contains ``day``."""
    return day.year if (day.month, day.day) >= (start_month, start_day) else day.year - 1


def year_label(year: int, start_month: int = 1, start_day: int = 1) -> str:
    """``2026`` for a calendar year, ``2026/27`` for one that starts later in the year."""
    if (start_month, start_day) == (1, 1):
        return str(year)
    return f"{year}/{(year + 1) % 100:02d}"


def period_bounds(
    period_type: str,
    year: int,
    index: int | None,
    *,
    start_month: int = 1,
    start_day: int = 1,
    week_start: int = 1,
) -> tuple[date, date]:
    """First and last day of the period of accounting year ``year``.

    ``index`` is the week (1-53), month (1-12) or quarter (1-4) counted from the start of the
    accounting year. ``week_start`` is the weekday a week begins on (1 Monday to 7 Sunday).
    Raises ``ValueError`` for an unknown type or an index outside its range.
    """
    first, last = year_bounds(year, start_month, start_day)
    if period_type == "year":
        return first, last
    if period_type == "quarter":
        if index is None or not 1 <= index <= 4:
            raise ValueError("A quarter is 1 to 4.")
        start = _add_months(first, 3 * (index - 1))
        return start, _add_months(start, 3) - timedelta(days=1)
    if period_type == "month":
        if index is None or not 1 <= index <= 12:
            raise ValueError("A month is 1 to 12.")
        start = _add_months(first, index - 1)
        return start, _add_months(start, 1) - timedelta(days=1)
    if period_type == "week":
        if index is None or not 1 <= index <= 53:
            raise ValueError("A week is 1 to 53.")
        # Week 1 begins on the first week-start day on or after the first day of the year.
        begin = first + timedelta(days=(week_start - 1 - first.weekday()) % 7)
        start = begin + timedelta(weeks=index - 1)
        if start > last:
            raise ValueError(f"The accounting year has {(last - begin).days // 7 + 1} weeks.")
        return start, start + timedelta(days=6)
    raise ValueError("A period is a week, a month, a quarter or a year.")


def year_periods(
    period_type: str,
    year: int,
    *,
    start_month: int = 1,
    start_day: int = 1,
    week_start: int = 1,
) -> list[tuple[int, date, date]]:
    """``(index, first day, last day)`` of every period of the accounting year, in order."""
    result = []
    for index in range(1, PERIODS_PER_YEAR[period_type] + 1):
        try:
            start, end = period_bounds(
                period_type,
                year,
                index,
                start_month=start_month,
                start_day=start_day,
                week_start=week_start,
            )
        except ValueError:
            break
        result.append((index, start, end))
    return result


def _uk(day: date) -> str:
    return day.strftime("%d/%m/%Y")


def period_label(period_type: str, start: date, end: date) -> str:
    """``Week of 06/04/2026``, ``March 2026``, ``Q1 2026``, ``2026``; a legacy lock reads
    ``Through 31/08/2026``. A month, quarter or year that does not start on a calendar boundary
    reads as its dates."""
    if period_type == "week":
        return f"Week of {_uk(start)}"
    if period_type == "month" and start.day == 1:
        return f"{MONTHS[start.month - 1]} {start.year}"
    if period_type == "quarter" and start.day == 1 and start.month in (1, 4, 7, 10):
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    if period_type == "year" and (start.month, start.day) == (1, 1):
        return str(start.year)
    if period_type == "year":
        return year_label(start.year, start.month, start.day)
    if period_type in ("month", "quarter"):
        return f"{_uk(start)} to {_uk(end)}"
    return f"Through {end.strftime('%d/%m/%Y')}"


__all__ = [
    "GRANULARITY_PERIOD",
    "MONTHS",
    "PERIODS_PER_YEAR",
    "PERIOD_GRANULARITY",
    "accounting_year_of",
    "period_bounds",
    "period_label",
    "year_bounds",
    "year_label",
    "year_periods",
]
