"""Helpers for the settings, rates and clients tests."""

from collections.abc import Iterator
from contextlib import contextmanager
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


async def sql(statement: str, **params: Any) -> list[dict]:
    """Run raw SQL in its own committed transaction; returns the rows as dicts."""
    async with get_sessionmaker()() as session:
        result = await session.execute(text(statement), params)
        rows = [dict(r._mapping) for r in result] if result.returns_rows else []
        await session.commit()
    return rows


async def scalar(statement: str, **params: Any) -> Any:
    async with get_sessionmaker()() as session:
        return await session.scalar(text(statement), params)


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
