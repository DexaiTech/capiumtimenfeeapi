"""The database clock in SQL (MySQL).

``NOW()`` alone has whole-second precision in MySQL; the ``DATETIME(6)`` columns and
PostgreSQL's ``now()`` before them keep microseconds, so comparisons and stamped values use
``NOW(6)``.
"""

from datetime import datetime

from sqlalchemy import ColumnElement, func, literal_column

from app.db.types import UTCDateTime


def db_now() -> ColumnElement[datetime]:
    """``NOW(6)``: the current UTC time (every session runs in UTC) with microseconds."""
    return func.now(literal_column("6"), type_=UTCDateTime())
