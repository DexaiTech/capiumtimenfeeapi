"""Keyset pagination on ``(sort expression, id)`` for date-sorted lists (jobs, time, sheets).

``app.utils.pagination.paginate`` handles datetime and scalar keys on one ORM entity; these
helpers also take dates and computed sort expressions (e.g. ``coalesce(due_date, ...)``) and
statements that select extra columns.
"""

import uuid
from collections.abc import Callable, Sequence
from datetime import date
from typing import Any

from sqlalchemy import ColumnElement, Select, tuple_
from sqlalchemy.orm import InstrumentedAttribute

from app.core.exceptions import ValidationError
from app.utils.pagination import PageParams, decode_cursor, encode_cursor


def parse_date(value: Any) -> date:
    return date.fromisoformat(str(value))


def keyset_query[*Ts](
    stmt: Select[*Ts],
    *,
    sort_expr: ColumnElement[Any] | InstrumentedAttribute[Any],
    id_column: ColumnElement[uuid.UUID] | InstrumentedAttribute[uuid.UUID],
    params: PageParams,
    descending: bool,
    parse: Callable[[Any], Any] = lambda value: value,
) -> Select[*Ts]:
    """Add the cursor condition, the ordering and ``LIMIT limit + 1``."""
    if params.cursor:
        raw, last_id = decode_cursor(params.cursor)
        try:
            value = parse(raw)
        except (ValueError, TypeError) as exc:
            raise ValidationError.for_field("cursor", "Invalid cursor.") from exc
        key = tuple_(sort_expr, id_column)
        stmt = stmt.where(key < (value, last_id) if descending else key > (value, last_id))
    order = (sort_expr.desc(), id_column.desc()) if descending else (sort_expr, id_column)
    return stmt.order_by(*order).limit(params.limit + 1)


def keyset_finish[R](
    rows: Sequence[R], params: PageParams, key: Callable[[R], tuple[Any, uuid.UUID]]
) -> tuple[list[R], str | None]:
    """Trim the look-ahead row and build the next cursor from the last row kept."""
    items = list(rows)
    if len(items) <= params.limit:
        return items, None
    items = items[: params.limit]
    value, row_id = key(items[-1])
    if isinstance(value, date):
        value = value.isoformat()
    return items, encode_cursor(value, row_id)
