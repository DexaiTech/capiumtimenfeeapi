"""The calendar periods a practice can lock: a month, a quarter or a calendar year."""

import calendar
from datetime import date

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


def period_bounds(period_type: str, year: int, index: int | None) -> tuple[date, date]:
    """First and last day of the period. ``index`` is the month (1-12) or quarter (1-4).

    Raises ``ValueError`` for an unknown type or an index outside its range.
    """
    if period_type == "year":
        return date(year, 1, 1), date(year, 12, 31)
    if period_type == "quarter":
        if index is None or not 1 <= index <= 4:
            raise ValueError("A quarter is 1 to 4.")
        first = 3 * index - 2
        return date(year, first, 1), date(year, first + 2, calendar.monthrange(year, first + 2)[1])
    if period_type == "month":
        if index is None or not 1 <= index <= 12:
            raise ValueError("A month is 1 to 12.")
        return date(year, index, 1), date(year, index, calendar.monthrange(year, index)[1])
    raise ValueError("A period is a month, a quarter or a year.")


def period_label(period_type: str, start: date, end: date) -> str:
    """``March 2026``, ``Q1 2026``, ``2026``; a legacy lock reads ``Through 31/08/2026``."""
    if period_type == "month":
        return f"{MONTHS[start.month - 1]} {start.year}"
    if period_type == "quarter":
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    if period_type == "year":
        return str(start.year)
    return f"Through {end.strftime('%d/%m/%Y')}"
