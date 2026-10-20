"""Keyset (cursor) pagination on ``(sort column, id)``."""

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.exceptions import ValidationError

DEFAULT_LIMIT, MAX_LIMIT = 25, 100


@dataclass(frozen=True, slots=True)
class PageParams:
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


def encode_cursor(value: Any, row_id: uuid.UUID) -> str:
    if isinstance(value, datetime):
        value = {"dt": value.isoformat()}
    elif isinstance(value, Decimal):
        # A SQL SUM() of an integer (pence) column comes back as a Decimal; it is always a
        # whole number, so it round-trips through JSON as a plain int.
        value = int(value)
    raw = json.dumps({"v": value, "id": str(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[Any, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        value = data["v"]
        if isinstance(value, dict):
            value = datetime.fromisoformat(value["dt"])
        return value, uuid.UUID(data["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationError.for_field("cursor", "Invalid cursor.") from exc


async def paginate[T](
    session: AsyncSession,
    stmt: Select[T],
    *,
    sort_column: InstrumentedAttribute[Any],
    id_column: InstrumentedAttribute[uuid.UUID],
    params: PageParams,
    descending: bool = False,
) -> Page[T]:
    """Run ``stmt`` (selecting one ORM entity) as one page ordered by ``(sort_column, id)``.

    Fetches ``limit + 1`` rows to learn whether another page exists.
    """
    if params.cursor:
        value, row_id = decode_cursor(params.cursor)
        key = tuple_(sort_column, id_column)
        stmt = stmt.where(key < (value, row_id) if descending else key > (value, row_id))
    order = (sort_column.desc(), id_column.desc()) if descending else (sort_column, id_column)
    rows = list((await session.execute(stmt.order_by(*order).limit(params.limit + 1))).scalars())
    next_cursor = None
    if len(rows) > params.limit:
        rows = rows[: params.limit]
        last = rows[-1]
        next_cursor = encode_cursor(getattr(last, sort_column.key), getattr(last, id_column.key))
    return Page(items=rows, next_cursor=next_cursor)
