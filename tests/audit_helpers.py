"""Helpers for the audit-log tests: the endpoints, entries written straight into the trail, and
what the trail holds for an action."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from httpx import AsyncClient, Response

from tests.clients_helpers import sql
from tests.factories import API, Actor

__all__ = [
    "audit_url",
    "entry_rows",
    "export",
    "history",
    "insert_entry",
    "listing",
]


def audit_url(org: dict, path: str = "") -> str:
    return f"{API}/organizations/{org['id']}/audit-logs{path}"


async def listing(client: AsyncClient, actor: Actor, org: dict, **params: Any) -> Response:
    return await client.get(audit_url(org), headers=actor.headers, params=params)


async def history(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    target_type: str,
    target_id: uuid.UUID | str,
    **params: Any,
) -> Response:
    return await client.get(
        audit_url(org, "/history"),
        headers=actor.headers,
        params={"target_type": target_type, "target_id": str(target_id), **params},
    )


async def export(
    client: AsyncClient, actor: Actor, org: dict, fmt: str = "csv", **params: Any
) -> Response:
    return await client.get(
        audit_url(org, "/export"), headers=actor.headers, params={"format": fmt, **params}
    )


async def insert_entry(
    org: dict,
    action: str,
    *,
    actor: Actor | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | str | None = None,
    meta: dict[str, Any] | None = None,
    created_at: datetime | None = None,
    ip: str | None = "203.0.113.7",
    user_agent: str | None = "Mozilla/5.0 (test)",
) -> str:
    """One entry written straight into the trail (the services write the real ones)."""
    entry_id = str(uuid.uuid4())
    await sql(
        "INSERT INTO audit_logs (id, created_at, organization_id, actor_user_id, action, "
        "target_type, target_id, ip_address, user_agent, request_id, metadata) VALUES "
        "(:id, :at, :org, :actor, :action, :tt, :tid, CAST(:ip AS inet), :ua, :rid, "
        "CAST(:meta AS jsonb))",
        id=entry_id,
        at=created_at or datetime.now(UTC),
        org=org["id"],
        actor=str(actor.id) if actor else None,
        action=action,
        tt=target_type,
        tid=str(target_id) if target_id else None,
        ip=ip,
        ua=user_agent,
        rid="req-" + entry_id[:8],
        meta=json.dumps(meta or {}),
    )
    return entry_id


async def entry_rows(org: dict, action: str | None = None, **where: Any) -> list[dict]:
    """The trail of the practice, oldest first: the columns a test asserts on, the address as
    text (without a mask) and ``metadata`` as a dict."""
    clauses = "".join(f" AND {column} = :{column}" for column in where)
    rows = await sql(
        "SELECT action, actor_user_id, target_type, target_id, host(ip_address) AS ip, "  # noqa: S608
        "user_agent, request_id, metadata FROM audit_logs "
        "WHERE organization_id = :org AND (CAST(:action AS text) IS NULL OR action = :action)"
        f"{clauses} ORDER BY created_at, id",
        org=org["id"],
        action=action,
        **where,
    )
    return [
        {**row, "actor_user_id": str(row["actor_user_id"]) if row["actor_user_id"] else None}
        | {"target_id": str(row["target_id"]) if row["target_id"] else None}
        for row in rows
    ]
