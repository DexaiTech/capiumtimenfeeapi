"""Helpers for the tests of time entry fields, copying, corrections, targets and reminders."""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update

from app.db.models import OrganizationMember
from app.db.session import get_sessionmaker
from tests.clients_helpers import sql
from tests.factories import Actor
from tests.time_helpers import MONDAY, log_time, ok_data, org_url

NEXT_MONDAY = MONDAY + timedelta(days=7)


def day(offset: int, monday: date = MONDAY) -> str:
    """ISO date ``offset`` days after ``monday`` (default: the Monday of the test week)."""
    return (monday + timedelta(days=offset)).isoformat()


def _plain(body: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, uuid.UUID | date) else v) for k, v in body.items()}


def pin_today(monkeypatch: pytest.MonkeyPatch, today: date) -> None:
    """Make the practice clock say it is noon UTC on ``today`` (London time is UTC or UTC+1)."""
    moment = datetime(today.year, today.month, today.day, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("app.services.time_rules.practice_now", lambda: moment)


async def set_member(org: dict, user: Actor, **values: Any) -> None:
    """Change a staff member's record directly (capacity, working days, dates, status...)."""
    async with get_sessionmaker()() as session:
        await session.execute(
            update(OrganizationMember)
            .where(
                OrganizationMember.organization_id == uuid.UUID(org["id"]),
                OrganizationMember.user_id == user.id,
            )
            .values(**values)
        )
        await session.commit()


async def sheet_action(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    sheet_id: str | uuid.UUID,
    action: str,
    *,
    status: int = 200,
    json: dict[str, Any] | None = None,
) -> Any:
    response = await client.post(
        org_url(org, f"/timesheets/{sheet_id}/{action}"), headers=actor.headers, json=json
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if status < 300 else response.json()["error"]


async def approve_sheet(
    client: AsyncClient, org: dict, person: Actor, approver: Actor, sheet_id: str | uuid.UUID
) -> None:
    """The person submits the sheet and the approver approves it."""
    await sheet_action(client, person, org, sheet_id, "submit")
    await sheet_action(client, approver, org, sheet_id, "approve")


async def approved_entry(
    client: AsyncClient, org: dict, person: Actor, approver: Actor, **body: Any
) -> dict[str, Any]:
    """An entry logged by ``person`` whose timesheet is submitted and approved by ``approver``."""
    entry = await log_time(client, person, org, **body)
    await approve_sheet(client, org, person, approver, entry["timesheet_id"])
    return await get_entry(client, approver, org, entry["id"])


async def get_entry(
    client: AsyncClient, actor: Actor, org: dict, entry_id: str | uuid.UUID
) -> dict[str, Any]:
    response = await client.get(org_url(org, f"/time-entries/{entry_id}"), headers=actor.headers)
    return ok_data(response)


async def get_sheet(
    client: AsyncClient, actor: Actor, org: dict, sheet_id: str | uuid.UUID
) -> dict[str, Any]:
    response = await client.get(org_url(org, f"/timesheets/{sheet_id}"), headers=actor.headers)
    return ok_data(response)


async def correct(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    entry_id: str | uuid.UUID,
    *,
    status: int = 200,
    **body: Any,
) -> Any:
    response = await client.post(
        org_url(org, f"/time-entries/{entry_id}/correct"), headers=actor.headers, json=_plain(body)
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if status < 300 else response.json()["error"]


async def history(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    entry_id: str | uuid.UUID,
    *,
    status: int = 200,
    **params: Any,
) -> Any:
    response = await client.get(
        org_url(org, f"/time-entries/{entry_id}/history"), headers=actor.headers, params=params
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if status < 300 else response.json()["error"]


async def history_rows(org: dict, entry_id: str | uuid.UUID) -> list[dict[str, Any]]:
    return await sql(
        "SELECT * FROM time_entry_history WHERE organization_id = :o AND time_entry_id = :e "
        "ORDER BY created_at, id",
        o=org["id"],
        e=str(entry_id),
    )


async def copy_week(
    client: AsyncClient, actor: Actor, org: dict, *, status: int = 200, **body: Any
) -> Any:
    response = await client.post(
        org_url(org, "/time-entries/copy-week"), headers=actor.headers, json=_plain(body)
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if status < 300 else response.json()["error"]


async def copy_entry(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    entry_id: str | uuid.UUID,
    *,
    status: int = 201,
    **body: Any,
) -> Any:
    response = await client.post(
        org_url(org, f"/time-entries/{entry_id}/copy"), headers=actor.headers, json=_plain(body)
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if status < 300 else response.json()["error"]


async def missing(
    client: AsyncClient, actor: Actor, org: dict, *, status: int = 200, **params: Any
) -> Any:
    response = await client.get(
        org_url(org, "/timesheets/missing"), headers=actor.headers, params=_plain(params)
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if status < 300 else response.json()["error"]


async def run_reminders(
    client: AsyncClient, actor: Actor, org: dict, *, status: int = 200, **body: Any
) -> Any:
    response = await client.post(
        org_url(org, "/timesheets/reminders/run"), headers=actor.headers, json=_plain(body)
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if status < 300 else response.json()["error"]


async def reminder_rows(org: dict, kind: str | None = None) -> list[dict[str, Any]]:
    return await sql(
        "SELECT user_id, week_start, kind, reminded_at FROM timesheet_reminders "
        "WHERE organization_id = :o AND (CAST(:k AS text) IS NULL OR kind = :k) "
        "ORDER BY week_start, kind, user_id",
        o=org["id"],
        k=kind,
    )
