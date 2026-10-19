"""The period rules of the dashboard: windows, comparisons and the weeks a card counts."""

from datetime import date, timedelta
from itertools import pairwise
from types import SimpleNamespace

from app.services.dashboard_common import compare, resolve_period, target_between
from app.services.dashboard_missing import MAX_WEEKS, weeks_of_window
from app.services.dashboard_widgets import _month_starts
from app.services.time_targets import WorkPattern


def test_this_week_starts_on_the_practice_week_start() -> None:
    monday = resolve_period("this_week", date(2026, 10, 12), 1)
    assert (monday.date_from, monday.date_to) == (date(2026, 10, 12), date(2026, 10, 12))
    assert (monday.previous_from, monday.previous_to) == (date(2026, 10, 5), date(2026, 10, 5))
    assert monday.horizon == date(2026, 10, 18)
    assert monday.before_start == date(2026, 10, 11)

    sunday_start = resolve_period("this_week", date(2026, 10, 17), 7)
    assert sunday_start.date_from == date(2026, 10, 11)
    assert (sunday_start.previous_from, sunday_start.previous_to) == (
        date(2026, 10, 4),
        date(2026, 10, 10),
    )
    assert sunday_start.horizon == date(2026, 10, 17)

    # The last day of the week: Sunday 18 October with Monday weeks.
    last = resolve_period("this_week", date(2026, 10, 18), 1)
    assert (last.date_from, last.previous_to) == (date(2026, 10, 12), date(2026, 10, 11))


def test_this_month_compares_the_same_number_of_days() -> None:
    mid = resolve_period("this_month", date(2026, 10, 17), 1)
    assert (mid.date_from, mid.previous_from, mid.previous_to) == (
        date(2026, 10, 1),
        date(2026, 9, 1),
        date(2026, 9, 17),
    )
    assert mid.horizon == date(2026, 10, 31)
    # A shorter month before: the 31st is compared with the end of April, February is 28 or 29.
    assert resolve_period("this_month", date(2026, 5, 31), 1).previous_to == date(2026, 4, 30)
    assert resolve_period("this_month", date(2027, 3, 31), 1).previous_to == date(2027, 2, 28)
    assert resolve_period("this_month", date(2028, 3, 31), 1).previous_to == date(2028, 2, 29)
    january = resolve_period("this_month", date(2027, 1, 20), 1)
    assert (january.previous_from, january.previous_to) == (date(2026, 12, 1), date(2026, 12, 20))
    first = resolve_period("this_month", date(2026, 3, 1), 1)
    assert (first.previous_from, first.previous_to) == (date(2026, 2, 1), date(2026, 2, 1))


def test_year_to_date_is_the_calendar_year() -> None:
    window = resolve_period("ytd", date(2026, 10, 17), 1)
    assert (window.date_from, window.date_to, window.horizon) == (
        date(2026, 1, 1),
        date(2026, 10, 17),
        date(2026, 10, 17),
    )
    assert (window.previous_from, window.previous_to) == (date(2025, 1, 1), date(2025, 10, 17))
    assert window.before_start == date(2025, 12, 31)
    # 29 February has no equal day the year before.
    leap = resolve_period("ytd", date(2028, 2, 29), 1)
    assert leap.previous_to == date(2027, 2, 28)
    new_year = resolve_period("ytd", date(2026, 1, 1), 1)
    assert (new_year.date_from, new_year.date_to) == (date(2026, 1, 1), date(2026, 1, 1))
    assert (new_year.previous_from, new_year.previous_to) == (date(2025, 1, 1), date(2025, 1, 1))


def test_comparisons_round_half_up_in_basis_points() -> None:
    up = compare("previous_period", 125, 100)
    assert (up.change, up.change_bp, up.basis) == (25, 2_500, "previous_period")
    down = compare("period_start", 1, 8)
    assert (down.previous, down.change, down.change_bp) == (8, -7, -8_750)
    assert compare("period_start", 1, 3).change_bp == -6_667
    assert compare("period_start", 2, 3).change_bp == -3_333
    assert compare("period_start", 7, 6).change_bp == 1_667
    assert compare("period_start", 0, 0).change_bp is None
    nothing = compare("previous_period", 5, 0)
    assert (nothing.change, nothing.change_bp) == (5, None)


def test_the_target_adds_up_the_working_days() -> None:
    pattern = WorkPattern(2_250, (1, 2, 3, 4, 5), date(2026, 9, 14))
    # 14 September is the first day: the weeks before it have no target.
    assert target_between(pattern, date(2026, 9, 1), date(2026, 9, 20)) == 5 * 450
    assert target_between(pattern, date(2026, 10, 12), date(2026, 10, 18)) == 2_250
    assert target_between(pattern, date(2026, 10, 17), date(2026, 10, 18)) == 0


def test_a_card_counts_the_completed_weeks_of_its_window() -> None:
    practice = SimpleNamespace(week_start=1)
    today = date(2026, 10, 17)
    assert weeks_of_window(practice, date(2026, 10, 12), today) == [date(2026, 10, 5)]
    # The week of 28 September ends on 4 October, inside October.
    assert weeks_of_window(practice, date(2026, 10, 1), today) == [
        date(2026, 9, 28),
        date(2026, 10, 5),
    ]
    year = weeks_of_window(practice, date(2026, 1, 1), today)
    assert (year[0], year[-1], len(year)) == (date(2025, 12, 29), date(2026, 10, 5), 41)
    assert all(b - a == timedelta(days=7) for a, b in pairwise(year))
    assert len(weeks_of_window(practice, date(1900, 1, 1), today)) == MAX_WEEKS
    # With Sunday weeks the week that just ended is 4-10 October.
    assert weeks_of_window(SimpleNamespace(week_start=7), date(2026, 10, 11), today) == [
        date(2026, 10, 4)
    ]


def test_the_months_of_a_trend() -> None:
    assert _month_starts(date(2026, 10, 17), 3) == [
        date(2026, 8, 1),
        date(2026, 9, 1),
        date(2026, 10, 1),
    ]
    assert _month_starts(date(2027, 1, 20), 3) == [
        date(2026, 11, 1),
        date(2026, 12, 1),
        date(2027, 1, 1),
    ]
    assert _month_starts(date(2026, 3, 31), 1) == [date(2026, 3, 1)]
    assert len(_month_starts(date(2026, 10, 17), 24)) == 24
