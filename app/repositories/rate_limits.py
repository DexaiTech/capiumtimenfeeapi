from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RateLimitCounter


class RateLimitRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def increment(self, key: str, window_start: datetime) -> int:
        stmt = (
            insert(RateLimitCounter)
            .values(key=key, window_start=window_start, count=1)
            .on_conflict_do_update(
                index_elements=["key", "window_start"],
                set_={"count": RateLimitCounter.count + 1},
            )
            .returning(RateLimitCounter.count)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def count(self, key: str, window_start: datetime) -> int:
        stmt = select(RateLimitCounter.count).where(
            RateLimitCounter.key == key, RateLimitCounter.window_start == window_start
        )
        return int((await self.session.execute(stmt)).scalar_one_or_none() or 0)

    async def purge(self, older_than: timedelta) -> int:
        result: CursorResult[tuple[()]] = await self.session.execute(  # type: ignore[assignment]
            delete(RateLimitCounter).where(RateLimitCounter.window_start < func.now() - older_than)
        )
        return result.rowcount
