"""Fixed-window rate limiting backed by PostgreSQL, shared by every API process."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.exceptions import RateLimitError
from app.repositories.rate_limits import RateLimitRepository


class ScaledRateLimiter:
    """A limiter whose limits are multiplied by a fixed factor (see ``auth_rate_limit_factor``)."""

    def __init__(self, limiter: "RateLimiter", factor: int) -> None:
        self._limiter = limiter
        self._factor = factor

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> None:
        await self._limiter.hit(key, limit=limit * self._factor, window_seconds=window_seconds)


class RateLimiter:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker
        # The counter upsert is one atomic statement, so it runs in autocommit mode: one
        # database round trip instead of BEGIN, upsert, COMMIT. It shares the pool.
        bind = sessionmaker.kw.get("bind")
        self._autocommit: AsyncEngine | None = (
            bind.execution_options(isolation_level="AUTOCOMMIT")
            if isinstance(bind, AsyncEngine)
            else None
        )

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> None:
        """Count one attempt; raise ``RateLimitError`` when the window is exhausted.

        Committed on its own (autocommit) so the count survives a rollback of the request.
        """
        now = datetime.now(UTC)
        epoch = int(now.timestamp())
        window_start = datetime.fromtimestamp(epoch - epoch % window_seconds, UTC)
        session_factory = (
            self._sessionmaker(bind=self._autocommit) if self._autocommit else self._sessionmaker()
        )
        async with session_factory as session:
            count = await RateLimitRepository(session).increment(key, window_start)
            await session.commit()
        if count > limit:
            raise RateLimitError(retry_after=max(1, window_seconds - epoch % window_seconds))

    async def is_exhausted(self, key: str, *, limit: int, window_seconds: int) -> int | None:
        """Without counting: seconds until the window ends when ``limit`` attempts were
        already recorded in it, else ``None``. For limits that count only failures."""
        epoch = int(datetime.now(UTC).timestamp())
        window_start = datetime.fromtimestamp(epoch - epoch % window_seconds, UTC)
        async with self._sessionmaker() as session:
            count = await RateLimitRepository(session).count(key, window_start)
        if count >= limit:
            return max(1, window_seconds - epoch % window_seconds)
        return None
