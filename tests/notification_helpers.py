"""Helpers for the notification, notification-settings, email-template and messaging tests."""

import uuid
from collections.abc import Iterable
from typing import Any

from httpx import AsyncClient

from app.core.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.services.notifications import NotificationService, NotifyResult
from app.workers.runner import Worker
from tests.clients_helpers import sql
from tests.factories import Actor
from tests.time_helpers import ok_data, org_url

__all__ = [
    "inbox",
    "message_log",
    "mock_settings",
    "mock_worker",
    "notification_jobs",
    "notify",
    "own_settings",
    "practice_settings",
    "put_own",
    "put_practice",
    "settings_with",
    "unread",
]


def _ids(recipients: Iterable[Actor | str | uuid.UUID]) -> list[uuid.UUID]:
    return [
        r.id if isinstance(r, Actor) else (r if isinstance(r, uuid.UUID) else uuid.UUID(str(r)))
        for r in recipients
    ]


async def notify(
    org: dict,
    type_: str,
    recipients: Iterable[Actor | str | uuid.UUID],
    *,
    title: str = "Something happened",
    **kwargs: Any,
) -> NotifyResult:
    """``NotificationService.notify`` in a transaction of its own, as a module would call it."""
    async with get_sessionmaker()() as session:
        result = await NotificationService(session).notify(
            organization_id=uuid.UUID(org["id"]),
            type=type_,
            recipients=_ids(recipients),
            title=title,
            **kwargs,
        )
        await session.commit()
    return result


async def inbox(client: AsyncClient, actor: Actor, org: dict, **params: Any) -> list[dict]:
    response = await client.get(
        org_url(org, "/notifications"), headers=actor.headers, params=params
    )
    return ok_data(response)


async def unread(client: AsyncClient, actor: Actor, org: dict) -> int:
    response = await client.get(org_url(org, "/notifications/unread-count"), headers=actor.headers)
    return int(ok_data(response)["count"])


async def notification_jobs(channel: str | None = None) -> list[dict]:
    """The payloads of the queued ``send_notification`` jobs, oldest first."""
    rows = await sql(
        "SELECT payload FROM background_jobs WHERE type = 'send_notification' ORDER BY created_at, id"
    )
    payloads = [row["payload"] for row in rows]
    return [p for p in payloads if channel is None or p["channel"] == channel]


async def message_log(**filters: Any) -> list[dict]:
    """What the mock gateway recorded, oldest first (``channel=``, ``recipient=``, ...)."""
    where = " AND ".join(f"{name} = :{name}" for name in filters) or "true"
    return await sql(
        f"SELECT * FROM message_log WHERE {where} ORDER BY created_at, id",  # noqa: S608
        **filters,
    )


async def own_settings(client: AsyncClient, actor: Actor, org: dict) -> dict:
    response = await client.get(org_url(org, "/notification-settings"), headers=actor.headers)
    return ok_data(response)


async def put_own(
    client: AsyncClient, actor: Actor, org: dict, body: dict, status: int = 200
) -> Any:
    response = await client.put(
        org_url(org, "/notification-settings"), headers=actor.headers, json=body
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if status == 200 else response.json()["error"]


async def practice_settings(client: AsyncClient, actor: Actor, org: dict) -> dict:
    response = await client.get(
        org_url(org, "/notification-settings/practice"), headers=actor.headers
    )
    return ok_data(response)


async def put_practice(
    client: AsyncClient, actor: Actor, org: dict, body: dict, status: int = 200
) -> Any:
    response = await client.put(
        org_url(org, "/notification-settings/practice"), headers=actor.headers, json=body
    )
    assert response.status_code == status, response.text
    return response.json()["data"] if status == 200 else response.json()["error"]


def settings_with(**overrides: Any) -> Settings:
    base = get_settings().model_dump()
    base.update(overrides)
    return Settings.model_validate(base)


def mock_settings(**overrides: Any) -> Settings:
    """The test settings with SMTP off, so the worker's gateway is the recording mock."""
    return settings_with(**{"smtp_host": "", **overrides})


def mock_worker() -> Worker:
    return Worker(get_sessionmaker(), mock_settings(), worker_id="mock-worker")
