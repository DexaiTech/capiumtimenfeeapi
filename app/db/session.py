"""Engine, session factory, request session dependency and the unit of work."""

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

import certifi
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.db import mysql_sql  # noqa: F401  (MySQL spellings, registered before use)
from app.db.errors import as_integrity_error

_UOW_DEPTH = "uow_depth"

# Every connection: UTC (``UTCDateTime`` relies on it) and strict SQL, whatever the server's
# own defaults are. Isolation is READ COMMITTED (``engine_options``), PostgreSQL's default,
# which the application's locking was written for: no gap locks, each statement sees what
# was committed before it.
SESSION_INIT = (
    "SET time_zone = '+00:00', "
    "sql_mode = 'ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
    "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION'"
)


def ssl_context(settings: Settings) -> ssl.SSLContext | None:
    """TLS for the MySQL connection from ``DB_SSL_MODE`` / ``DB_SSL_CA``; None when disabled."""
    mode = settings.db_ssl_mode
    if mode == "disabled":
        return None
    context = ssl.create_default_context(cafile=settings.db_ssl_ca.strip() or certifi.where())
    if mode == "required":
        # Encrypted, but the server's certificate is not checked.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif mode == "verify_ca":
        context.check_hostname = False
    return context


def engine_options(settings: Settings) -> dict[str, Any]:
    """Keyword arguments for ``create_async_engine`` shared by the app, Alembic and tests."""
    connect_args: dict[str, Any] = {"init_command": SESSION_INIT}
    context = ssl_context(settings)
    if context is not None:
        connect_args["ssl"] = context
    return {"isolation_level": "READ COMMITTED", "connect_args": connect_args}


def new_engine(url: str, settings: Settings, **kwargs: Any) -> AsyncEngine:
    """An engine with the application's connection settings and error rules. The app, the
    maintenance commands and the tests all make theirs here."""
    engine = create_async_engine(url, **engine_options(settings), **kwargs)
    event.listen(engine.sync_engine, "handle_error", as_integrity_error)
    return engine


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    return new_engine(
        settings.database_url,
        settings,
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
