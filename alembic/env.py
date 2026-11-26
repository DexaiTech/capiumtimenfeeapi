"""Alembic environment: async engine, URL from application settings."""

import asyncio
from typing import Any

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db import models  # noqa: F401  (registers every table on Base.metadata)
from app.db.base import Base
from app.db.session import engine_options
from app.db.types import UTCDateTime, Uuid36

target_metadata = Base.metadata

# Alembic's own bookkeeping table is not a model.
_NOT_MODELS = ("alembic_version",)


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    return not (type_ == "table" and reflected and name in _NOT_MODELS)


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
    """Migrations spell the app's own types as the columns MySQL holds, never importing them."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        autogen_context.imports.add("from sqlalchemy.dialects import mysql")
        return "mysql.DATETIME(fsp=6)"
    if type_ == "type" and isinstance(obj, Uuid36):
        autogen_context.imports.add("from sqlalchemy.dialects import mysql")
        return 'mysql.CHAR(36, charset="ascii", collation="ascii_bin")'
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().direct_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    settings = get_settings()
    engine = create_async_engine(
        settings.direct_database_url, poolclass=NullPool, **engine_options(settings)
    )
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
