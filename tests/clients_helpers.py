"""Helpers for the settings, rates and clients tests."""

import json
import re
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from httpx import AsyncClient
from sqlalchemy import event, text

from app.db.session import get_engine, get_sessionmaker
from tests.factories import API, Actor

__all__ = ["API", "count_queries", "create_client", "org_url", "scalar", "sql"]


def org_url(org: dict, path: str = "") -> str:
    return f"{API}/organizations/{org['id']}{path}"


async def create_client(
    client: AsyncClient, actor: Actor, org: dict, name: str = "Acme Widgets Ltd", **body: Any
) -> dict:
    """Create a client through the API; returns ``data`` (the client overview)."""
    response = await client.post(
        org_url(org, "/clients"), headers=actor.headers, json={"name": name, **body}
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


# MySQL type codes of the cursor description (pymysql FIELD_TYPE).
_JSON, _DATETIME, _TINY = 245, 12, 1
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _python(value: Any, column: Sequence[Any]) -> Any:
    """A raw MySQL value as the PostgreSQL driver gave it, so tests read the same types: decoded
    JSON, aware UTC datetimes, booleans and ``uuid.UUID`` ids."""
    if value is None:
        return None
    type_code, size = column[1], column[3]
    if type_code == _JSON and isinstance(value, str | bytes):
        return json.loads(value)
    if type_code == _DATETIME and isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    if type_code == _TINY and size == 1 and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and _UUID.match(value):
        return uuid.UUID(value)
    return value


def _rows(result: Any) -> list[dict]:
    description = result.cursor.description if result.cursor is not None else None
    rows = []
    for row in result:
        values = dict(row._mapping)
        if description:
            values = {
                key: _python(value, column)
                for (key, value), column in zip(values.items(), description, strict=True)
            }
        rows.append(values)
    return rows


async def sql(statement: str, **params: Any) -> list[dict]:
    """Run raw SQL in its own committed transaction; returns the rows as dicts."""
    async with get_sessionmaker()() as session:
        result = await session.execute(text(statement), params)
        rows = _rows(result) if result.returns_rows else []
        await session.commit()
    return rows


async def scalar(statement: str, **params: Any) -> Any:
    async with get_sessionmaker()() as session:
        rows = _rows(await session.execute(text(statement), params))
    return next(iter(rows[0].values())) if rows else None


@contextmanager
def count_queries() -> Iterator[list[str]]:
    """Collect the SQL statements the application sends while the block runs."""
    statements: list[str] = []

    def before(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before)
