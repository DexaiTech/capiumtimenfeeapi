"""Helpers for the tests of jobs, tasks, the job calendar, budget tracking and budget alerts."""

import uuid
from datetime import date
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import select

from app.db.models import Client, Job, Permission, Role, RolePermission
from app.db.session import get_sessionmaker
from tests.clients_helpers import sql
from tests.domain_factories import staff
from tests.factories import Actor
from tests.notification_helpers import inbox
from tests.time_helpers import log_time, org_url

__all__ = [
    "add_task",
    "alert_keys",
    "budget_notices",
    "create_job",
    "job_row",
    "job_url",
    "log_on_job",
    "role_without",
    "set_job_row",
    "task_url",
]


def job_url(org: dict, job_id: str | uuid.UUID, suffix: str = "") -> str:
    return org_url(org, f"/jobs/{job_id}{suffix}")


def task_url(
    org: dict, job_id: str | uuid.UUID, task_id: str | uuid.UUID = "", suffix: str = ""
) -> str:
    base = f"/jobs/{job_id}/tasks"
    return org_url(org, f"{base}/{task_id}{suffix}" if task_id else f"{base}{suffix}")


def _plain(body: dict[str, Any]) -> dict[str, Any]:
    """A request body with ids and dates as strings."""
    return {
        key: [str(v) if isinstance(v, uuid.UUID) else v for v in value]
        if isinstance(value, list)
        else (str(value) if isinstance(value, uuid.UUID | date) else value)
        for key, value in body.items()
    }


def _data(response: Response, status: int) -> Any:
    assert response.status_code == status, response.text
    return response.json()["data"] if status < 300 else response.json()["error"]


async def create_job(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    customer: Client | str | uuid.UUID,
    name: str = "Accounts",
    *,
    expect: int = 201,
    **body: Any,
) -> Any:
    """``POST /jobs``: the job (or, when ``expect`` is another status, the error)."""
    client_id = customer.id if isinstance(customer, Client) else customer
    response = await client.post(
        org_url(org, "/jobs"),
        headers=actor.headers,
        json=_plain({"client_id": client_id, "name": name, **body}),
    )
    return _data(response, expect)


async def add_task(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    job: dict | str | uuid.UUID,
    title: str = "Prepare the accounts",
    *,
    expect: int = 201,
    **body: Any,
) -> Any:
    """``POST /jobs/{id}/tasks``: the task (or, when ``expect`` is another status, the error)."""
    job_id = job["id"] if isinstance(job, dict) else job
    response = await client.post(
        task_url(org, job_id), headers=actor.headers, json=_plain({"title": title, **body})
    )
    return _data(response, expect)


async def log_on_job(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    job: dict,
    minutes: int = 60,
    *,
    entry_date: str = "2026-09-07",
    status: int = 201,
    **body: Any,
) -> Any:
    """Time on a job through ``POST /time-entries`` (the job's client comes with it)."""
    return await log_time(
        client,
        actor,
        org,
        status=status,
        job_id=job["id"],
        client_id=job["client_id"],
        minutes=minutes,
        entry_date=entry_date,
        **body,
    )


async def job_row(job_id: str | uuid.UUID) -> Job:
    """A job as it is in the database."""
    async with get_sessionmaker()() as session:
        row = await session.get(Job, uuid.UUID(str(job_id)))
    assert row is not None
    return row


async def set_job_row(job_id: str | uuid.UUID, **values: Any) -> None:
    """Change a job directly in the database (a status, a budget, ...)."""
    assignments = ", ".join(f"{name} = :{name}" for name in values)
    await sql(f"UPDATE jobs SET {assignments} WHERE id = :job_id", job_id=str(job_id), **values)  # noqa: S608


async def alert_keys(job_id: str | uuid.UUID) -> list[tuple[str, int]]:
    """The ``(kind, threshold)`` alerts a job holds, in a stable order."""
    rows = await sql(
        "SELECT kind, threshold FROM job_budget_alerts WHERE job_id = :j ORDER BY kind, threshold",
        j=str(job_id),
    )
    return [(row["kind"], int(row["threshold"])) for row in rows]


async def budget_notices(client: AsyncClient, actor: Actor, org: dict) -> list[dict]:
    """The budget alerts in a person's inbox, oldest first."""
    return [n for n in reversed(await inbox(client, actor, org)) if n["type"] == "budget_alert"]


async def role_without(org: dict, code: str) -> Actor:
    """A member of a custom role that holds nothing but ``organization.read``."""
    async with get_sessionmaker()() as session:
        role = Role(organization_id=uuid.UUID(org["id"]), name=f"No {code}")
        session.add(role)
        await session.flush()
        permission = await session.scalar(
            select(Permission).where(Permission.code == "organization.read")
        )
        assert permission is not None
        session.add(RolePermission(role_id=role.id, permission_id=permission.id))
        await session.commit()
        name = role.name
    return await staff(org, name)
