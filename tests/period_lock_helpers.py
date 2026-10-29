"""Helpers for the period-lock tests: dates relative to the practice's today, locking through the
API or straight in the database, and the rows the lock keeps.

A practice locks one length of period (weekly unless it chose otherwise): ``lock_period`` switches
the practice to the length of the period it is asked to lock first, so tests that only need a
locked period do not repeat the set-up.
"""

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
    "lock_period",
    "locked_periods",
    "org_url",
    "set_granularity",
    "set_lock",
    "today",
    "unlock_period",
]


def org_url(org: dict, path: str) -> str:
    return f"{API}/organizations/{org['id']}{path}"


def today(org: dict) -> date:
    """Today in the practice's time zone, the day the service measures "not in the future" by."""
    return today_in(org["timezone"])


_GRANULARITY = {"week": "weekly", "month": "monthly", "quarter": "quarterly", "year": "annually"}


async def set_granularity(
    org: dict, granularity: str, start: tuple[int, int] = (1, 1), *, configured: bool = True
) -> None:
    """Set how the practice locks straight in the database."""
    async with get_sessionmaker()() as session:
        await session.execute(
            text(
                "UPDATE organizations SET period_lock_granularity = :g, "
                "financial_year_start_month = :m, financial_year_start_day = :d, "
                "period_lock_configured = :c WHERE id = :org"
            ),
            {
                "g": granularity,
                "m": start[0],
                "d": start[1],
                "c": configured,
                "org": uuid.UUID(org["id"]),
            },
        )
        await session.commit()


async def lock_period(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    period_type: str,
    year: int,
    period: int | None = None,
    *,
    align: bool = True,
    **body: Any,
) -> Response:
    if align and period_type in _GRANULARITY:
        await set_granularity(org, _GRANULARITY[period_type])
    payload: dict[str, Any] = {"period_type": period_type, "year": year, **body}
    if period is not None:
        payload["period"] = period
    return await client.post(org_url(org, "/period-locks"), headers=actor.headers, json=payload)


async def unlock_period(
    client: AsyncClient, actor: Actor, org: dict, lock_id: str, **body: Any
) -> Response:
    return await client.post(
        org_url(org, "/period-locks/reopen"),
        headers=actor.headers,
        json={"lock_id": lock_id, **body},
    )


async def set_lock(org: dict, day: date | None) -> None:
    """Lock everything through ``day`` straight in the database (a ``legacy`` period, no event,
    no audit), or nothing for None: set-up for the enforcement tests."""
    async with get_sessionmaker()() as session:
        await session.execute(
            text("DELETE FROM locked_periods WHERE organization_id = :org"),
            {"org": uuid.UUID(org["id"])},
        )
        if day is not None:
            await session.execute(
                text(
                    "INSERT INTO locked_periods (id, organization_id, period_type, start_date, "
                    "end_date) VALUES (gen_random_uuid(), :org, 'legacy', DATE '2000-01-01', :day)"
                ),
                {"org": uuid.UUID(org["id"]), "day": day},
            )
        await session.commit()


async def locked_periods(org: dict) -> list[dict[str, Any]]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            text(
                "SELECT period_type, start_date, end_date FROM locked_periods "
                "WHERE organization_id = :org ORDER BY start_date"
            ),
            {"org": uuid.UUID(org["id"])},
        )
        return [dict(row._mapping) for row in rows]


async def events(org: dict) -> list[dict[str, Any]]:
    """The history rows, oldest first."""
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            text(
                "SELECT event_type, period_type, period_start, period_end, reason, "
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
