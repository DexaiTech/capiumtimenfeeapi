"""Paging of a report's rows: the whole report is computed, a window of its rows is returned."""

from collections.abc import Sequence

from app.schemas.reports import PagedQuery, ReportPage


def window[T](rows: Sequence[T], q: PagedQuery) -> tuple[list[T], ReportPage]:
    """The rows ``q.limit``/``q.offset`` select, and where they sit in the report.

    The rows are in the report's own, deterministic order, so successive pages never overlap or
    skip a row. A download (``format`` other than ``json``) always holds every row.
    """
    total = len(rows)
    if q.format != "json":
        return list(rows), ReportPage(
            total=total, limit=None, offset=0, has_more=False, next_offset=None
        )
    end = total if q.limit is None else q.offset + q.limit
    has_more = end < total
    page = ReportPage(
        total=total,
        limit=q.limit,
        offset=q.offset,
        has_more=has_more,
        next_offset=end if has_more else None,
    )
    return list(rows[q.offset : end]), page
