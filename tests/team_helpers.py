"""Helpers for the staff, grades, rates and re-rating tests."""

import uuid
from datetime import date
from typing import Any

from httpx import AsyncClient

from tests.clients_helpers import scalar, sql
from tests.factories import Actor
from tests.time_helpers import err, ok_data, org_url

__all__ = [
    "bill",
    "grade_id",
    "new_rate",
    "new_staff",
    "rate_error",
    "rule_count",
    "set_grade",
    "set_grade_rates",
]


def _plain(body: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, uuid.UUID | date) else v) for k, v in body.items()}


async def grade_id(org: dict, code: str = "SNR") -> str:
    """The id of one of the practice's default grades (PTR, MGR, SNR, JNR)."""
    found = await scalar(
        "SELECT id FROM grades WHERE organization_id = :o AND code = :c", o=org["id"], c=code
    )
    assert found is not None, f"no grade {code}"
    return str(found)


async def set_grade_rates(
    org: dict, code: str = "SNR", *, charge: int | None = None, cost: int | None = None
) -> None:
    await sql(
        "UPDATE grades SET default_charge_rate = :ch, default_cost_rate = :co "
        "WHERE organization_id = :o AND code = :c",
        o=org["id"],
        c=code,
        ch=charge,
        co=cost,
    )


async def set_grade(org: dict, user: Actor | str, code: str | None) -> None:
    """Give a staff member a grade straight in the database (None clears it)."""
    user_id = str(user.id) if isinstance(user, Actor) else user
    await sql(
        "UPDATE organization_members SET grade_id = (SELECT id FROM grades "
        "WHERE organization_id = :o AND code = :c) WHERE organization_id = :o AND user_id = :u",
        o=org["id"],
        c=code,
        u=user_id,
    )


async def new_staff(
    client: AsyncClient, actor: Actor, org: dict, name: str = "Sam Staff", **body: Any
) -> dict:
    """Create a staff member (no login) through the API; returns ``data``."""
    body.setdefault("grade_id", await grade_id(org))
    body.setdefault("start_date", "2026-01-05")
    response = await client.post(
        org_url(org, "/staff"), headers=actor.headers, json=_plain({"full_name": name, **body})
    )
    return ok_data(response, 201)


async def new_rate(client: AsyncClient, actor: Actor, org: dict, **body: Any) -> dict:
    """Add a rate through the API (201); returns the rate."""
    response = await client.post(org_url(org, "/rates"), headers=actor.headers, json=_plain(body))
    return ok_data(response, 201)


async def rate_error(
    client: AsyncClient, actor: Actor, org: dict, status: int, **body: Any
) -> dict:
    """Add a rate that must be refused with ``status``; returns the error."""
    response = await client.post(org_url(org, "/rates"), headers=actor.headers, json=_plain(body))
    return err(response, status)


async def rule_count(org: dict, **where: Any) -> int:
    clauses = "".join(f" AND {column} = :{column}" for column in where)
    found = await scalar(
        f"SELECT count(*) FROM rate_rules WHERE organization_id = :o{clauses}",  # noqa: S608
        o=org["id"],
        **where,
    )
    return int(found or 0)


async def bill(entry_id: uuid.UUID | str, status: str = "billed") -> None:
    await sql(
        "UPDATE time_entries SET billing_status = :s WHERE id = :i", s=status, i=str(entry_id)
    )
