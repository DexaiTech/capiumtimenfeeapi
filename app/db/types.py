"""Column types and server defaults shared by every model (MySQL 8.4).

* ``UTCDateTime``: MySQL ``DATETIME(6)`` has no time zone. Every session runs with
  ``time_zone = '+00:00'`` (``app/db/session.py``); values are written as UTC and read back as
  aware UTC datetimes, so the application keeps working with aware datetimes only.
* ``Uuid36``: ids as the 36-character text PostgreSQL showed (``8c4f...-...``), in an ASCII binary
  column. Raw SQL, JSON metadata and text comparisons then see the same value the API returns.
* ``NOW``: the default for ``UTCDateTime`` columns. ``DATETIME(6)`` needs the same precision in
  its default (``CURRENT_TIMESTAMP(6)``); ``now()`` alone is refused.
* ``NOW_ON_UPDATE``: ``updated_at`` is kept current by MySQL itself on every change to the row.
* ``JSON_EMPTY_OBJECT`` / ``JSON_EMPTY_ARRAY``: MySQL only allows expression defaults on JSON.
"""

import uuid
from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import Dialect, TypeDecorator, text
from sqlalchemy.dialects.mysql import CHAR, DATETIME

NOW = text("CURRENT_TIMESTAMP(6)")
NOW_ON_UPDATE = text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)")
JSON_EMPTY_OBJECT = text("(JSON_OBJECT())")
JSON_EMPTY_ARRAY = text("(JSON_ARRAY())")


class UTCDateTime(TypeDecorator[datetime]):
    """An aware datetime stored as UTC in ``DATETIME(6)``.

    A naive value is taken to be UTC already, as PostgreSQL's ``timestamptz`` did in a UTC
    session, and a ``date`` is its midnight UTC, so existing callers keep their meaning.
    """

    impl = DATETIME(fsp=6)
    cache_ok = True

    def process_bind_param(
        self, value: datetime | date | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            # A date compared with a timestamp is its midnight UTC, as PostgreSQL cast it.
            return datetime.combine(value, time.min)
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        assert isinstance(value, datetime)
        return value.replace(tzinfo=UTC)


class Uuid36(TypeDecorator[uuid.UUID]):
    """A UUID stored as its canonical 36-character text in ``CHAR(36)`` (ASCII, binary)."""

    impl = CHAR(36, charset="ascii", collation="ascii_bin")
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return str(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))

    def process_result_value(self, value: Any, dialect: Dialect) -> uuid.UUID | None:
        return None if value is None else uuid.UUID(value)
