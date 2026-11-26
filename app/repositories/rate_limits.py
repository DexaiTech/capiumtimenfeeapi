from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RateLimitCounter


class RateLimitRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def increment(self, key: str, window_start: datetime) -> int:
        """The window's count after adding one, in one statement (as RETURNING was).

        The primary key (key, window_start) is the table's only unique key. ``LAST_INSERT_ID(x)``
        hands the new count back in the statement's own reply (the table has no
        auto-increment column, so nothing else sets it): 1 for a new row, ``count + 1`` when
        the row was there.
        """
        stmt = (
            insert(RateLimitCounter)
            .values(key=key, window_start=window_start, count=func.last_insert_id(1))
            .on_duplicate_key_update(count=func.last_insert_id(RateLimitCounter.count + 1))
        )
        result: CursorResult[Any] = await self.session.execute(stmt)  # type: ignore[assignment]
        return int(result.lastrowid)

    async def count(self, key: str, window_start: datetime) -> int:
        stmt = select(RateLimitCounter.count).where(
            RateLimitCounter.key == key, RateLimitCounter.window_start == window_start
        )
        return int((await self.session.execute(stmt)).scalar_one_or_none() or 0)

    async def purge(self, older_than: timedelta) -> int:
        result: CursorResult[tuple[()]] = await self.session.execute(  # type: ignore[assignment]
            delete(RateLimitCounter).where(
                RateLimitCounter.window_start < datetime.now(UTC) - older_than
            )
        )
        return result.rowcount
