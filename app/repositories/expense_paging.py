"""Keyset pagination for the expense lists: ordered by ``(sort key, id)``, with a cursor that
carries the sort it belongs to, so a cursor from another sort or direction answers 422 and a
tampered one never reaches the database as a mistyped parameter."""

import base64
import json
import uuid
from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy import ColumnElement, Row, Select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.exceptions import ValidationError
from app.utils.pagination import PageParams

type SortKind = Literal["dt", "date", "int", "str"]
type AnySelect = Select[*tuple[Any, ...]]
type AnyRow = Row[*tuple[Any, ...]]
type SortExpr = ColumnElement[Any] | InstrumentedAttribute[Any]
type IdExpr = ColumnElement[uuid.UUID] | InstrumentedAttribute[uuid.UUID]

_MAX_INT = 10**15
_MAX_TEXT = 300


def encode_cursor(
    sort: str, descending: bool, kind: SortKind, value: Any, row_id: uuid.UUID
) -> str:
    text = value.isoformat() if isinstance(value, datetime | date) else value
    if kind == "int":
        text = int(value)  # an aggregate may arrive as a Decimal
    payload = {"s": sort, "d": descending, "k": kind, "v": text, "id": str(row_id)}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _value(kind: SortKind, raw: Any) -> Any:
    if kind == "dt":
        if not isinstance(raw, str):
            raise ValueError("expected a timestamp")
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            raise ValueError("expected an aware timestamp")
        return parsed
    if kind == "date":
        if not isinstance(raw, str):
            raise ValueError("expected a date")
        return date.fromisoformat(raw)
    if kind == "int":
        if type(raw) is not int or not -_MAX_INT <= raw <= _MAX_INT:
            raise ValueError("expected a number")
        return raw
    if not isinstance(raw, str) or "\x00" in raw or len(raw) > _MAX_TEXT:
        raise ValueError("expected text")
    return raw


def decode_cursor(
    cursor: str, sort: str, descending: bool, kind: SortKind
) -> tuple[Any, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if data["s"] != sort or bool(data["d"]) != descending or data["k"] != kind:
            raise ValueError("cursor of another sort")
        if not isinstance(data["id"], str):
            raise ValueError("bad id")
        return _value(kind, data["v"]), uuid.UUID(data["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValidationError.for_field(
            "cursor", "Invalid cursor (the sort or filters may have changed).", "invalid_cursor"
        ) from exc


async def keyset_page(
    session: AsyncSession,
    stmt: AnySelect,
    *,
    key: SortExpr,
    id_column: IdExpr,
    sort: str,
    kind: SortKind,
    descending: bool,
    params: PageParams,
) -> tuple[list[AnyRow], str | None]:
    """One page of ``stmt`` ordered by ``(key, id)``. The rows carry the key as ``sort_key`` and
    the id as ``sort_id``; the second item is the cursor of the next page, or None."""
    labelled = key.label("sort_key")
    stmt = stmt.add_columns(labelled, id_column.label("sort_id"))
    if params.cursor:
        value, row_id = decode_cursor(params.cursor, sort, descending, kind)
        pair = tuple_(key, id_column)
        stmt = stmt.where(pair < (value, row_id) if descending else pair > (value, row_id))
    order = (labelled.desc(), id_column.desc()) if descending else (labelled.asc(), id_column.asc())
    rows = list((await session.execute(stmt.order_by(*order).limit(params.limit + 1))).all())
    if len(rows) <= params.limit:
        return rows, None
    rows = rows[: params.limit]
    last = rows[-1]
    return rows, encode_cursor(sort, descending, kind, last.sort_key, last.sort_id)
