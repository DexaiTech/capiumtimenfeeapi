"""Helpers for the period-lock tests: dates relative to the practice's today, locking through the
API or straight in the database, and the rows the lock keeps."""

import uuid
from datetime import date
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import text

from app.db.session import get_sessionmaker
from app.utils.practice_dates import today_in
from tests.factories import API, Actor

__all__ = [
    "assert_locked",
    "events",
    "lock_through",
    "locked_through",
    "org_url",
    "reopen_to",
    "set_lock",
    "today",
]


def org_url(org: dict, path: str) -> str:
    return f"{API}/organizations/{org['id']}{path}"


def today(org: dict) -> date:
    """Today in the practice's time zone, the day the service measures "not in the future" by."""
    return today_in(org["timezone"])


async def lock_through(
    client: AsyncClient, actor: Actor, org: dict, day: date | str, **body: Any
) -> Response:
    return await client.post(
        org_url(org, "/period-locks"),
        headers=actor.headers,
        json={"locked_through": str(day), **body},
    )


async def reopen_to(
    client: AsyncClient, actor: Actor, org: dict, day: date | str | None, **body: Any
) -> Response:
    return await client.post(
        org_url(org, "/period-locks/reopen"),
        headers=actor.headers,
        json={"reopen_to": None if day is None else str(day), **body},
    )


async def set_lock(org: dict, day: date | None) -> None:
    """Lock straight in the database (no event, no audit): set-up for the enforcement tests."""
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "INSERT INTO period_locks (id, organization_id, locked_through) "
                "VALUES (gen_random_uuid(), :org, :day) "
                "ON CONFLICT (organization_id) DO UPDATE SET locked_through = :day"
            ),
            {"org": uuid.UUID(org["id"]), "day": day},
        )
        await session.commit()


async def locked_through(org: dict) -> date | None:
    async with get_sessionmaker()() as session:
        return await session.scalar(
            text("SELECT locked_through FROM period_locks WHERE organization_id = :org"),
            {"org": uuid.UUID(org["id"])},
        )


async def events(org: dict) -> list[dict[str, Any]]:
    """The history rows, oldest first."""
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            text(
                "SELECT event_type, previous_locked_through, locked_through, reason, "
                "actor_user_id FROM period_lock_events WHERE organization_id = :org "
                "ORDER BY created_at, id"
            ),
            {"org": uuid.UUID(org["id"])},
        )
        return [dict(row._mapping) for row in rows]


def assert_locked(response: Response, on: date, locked_through_: date, area: str) -> None:
    """The response is the 409 ``PERIOD_LOCKED`` of a change dated ``on``."""
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "PERIOD_LOCKED", error
    assert error["details"] == {
        "date": on.isoformat(),
        "locked_through": locked_through_.isoformat(),
        "area": area,
    }
