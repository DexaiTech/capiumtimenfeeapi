"""Engine, session factory, request session dependency and the unit of work."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_UOW_DEPTH = "uow_depth"


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        # A pre-ping costs one database round trip on every checkout, which is what made
        # requests slow against a distant database. Connections are recycled before
        # servers or proxies drop idle ones instead; a connection that died anyway fails
        # that one request with 503 and is replaced.
        pool_pre_ping=settings.db_pool_pre_ping,
        pool_recycle=settings.db_pool_recycle_seconds,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """One session per request. Services commit through ``unit_of_work``."""
    async with get_sessionmaker()() as session:
        yield session


@asynccontextmanager
async def unit_of_work(session: AsyncSession) -> AsyncIterator[None]:
    """Commit on success, roll back on error. Re-entrant: inner blocks join the outer one."""
    depth = session.info.get(_UOW_DEPTH, 0)
    session.info[_UOW_DEPTH] = depth + 1
    try:
        yield
        if depth == 0:
            await session.commit()
    except BaseException:
        if depth == 0:
            await session.rollback()
        raise
    finally:
        session.info[_UOW_DEPTH] = depth


async def dispose_engine() -> None:
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
    get_sessionmaker.cache_clear()
    get_engine.cache_clear()
