"""Helpers for the billing tests (disbursements, WIP, invoices, credit notes, payments)."""

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import text

from app.db.models import Client, Job
from app.db.session import get_sessionmaker
from tests.domain_factories import make_client, make_job, practice
from tests.factories import API, Actor

__all__ = [
    "API",
    "Practice",
    "billing_status",
    "count_rows",
    "draft",
    "issue",
    "new_practice",
    "org_url",
    "pay",
    "set_org",
]


@dataclass
class Practice:
    owner: Actor
    org: dict
    client: Client

    @property
    def id(self) -> str:
        return str(self.org["id"])

    def url(self, path: str) -> str:
        return org_url(self.org, path)


def org_url(org: dict, path: str) -> str:
    return f"{API}/organizations/{org['id']}{path}"


async def set_org(org: dict, **fields: Any) -> None:
    """Change practice settings directly (vat_registered, require_timesheet_approval, ...)."""
    assignments = ", ".join(f"{name} = :{name}" for name in fields)
    async with get_sessionmaker()() as session:
        await session.execute(
            text(f"UPDATE organizations SET {assignments} WHERE id = :id"),  # noqa: S608
            {"id": str(org["id"]), **fields},
        )
        await session.commit()


async def new_practice(http: AsyncClient, name: str = "Acme Accountants", **org: Any) -> Practice:
    owner, organization = await practice(http, name)
    if org:
        await set_org(organization, **org)
    client = await make_client(organization, "Widgets Ltd")
    return Practice(owner=owner, org=organization, client=client)


async def hourly_job(p: Practice, client: Client | None = None, **fields: Any) -> Job:
    return await make_job(p.org, client or p.client, fields.pop("name", "Bookkeeping"), **fields)


async def billing_status(table: str, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            text(f"SELECT id, billing_status FROM {table} WHERE id = ANY(:ids)"),  # noqa: S608
            {"ids": [str(i) for i in ids]},
        )
        return {row[0]: row[1] for row in rows}


async def count_rows(sql: str, **params: Any) -> int:
    async with get_sessionmaker()() as session:
        return int((await session.execute(text(sql), params)).scalar_one())


async def draft(
    http: AsyncClient,
    actor: Actor,
    org: dict,
    client_id: uuid.UUID | str,
    *,
    time: list[Any] | None = None,
    disbursements: list[Any] | None = None,
    expect: int = 201,
    **body: Any,
) -> dict:
    response = await http.post(
        org_url(org, "/invoices"),
        headers=actor.headers,
        json={
            "client_id": str(client_id),
            "time_entry_ids": [str(getattr(t, "id", t)) for t in time or []],
            "disbursement_ids": [str(getattr(d, "id", d)) for d in disbursements or []],
            **body,
        },
    )
    assert response.status_code == expect, response.text
    return response.json()["data"] if expect == 201 else response.json()["error"]


async def issue(
    http: AsyncClient, actor: Actor, org: dict, invoice_id: str, **body: Any
) -> Response:
    return await http.post(
        org_url(org, f"/invoices/{invoice_id}/issue"), headers=actor.headers, json=body or None
    )


async def issued(http: AsyncClient, actor: Actor, org: dict, invoice_id: str, **body: Any) -> dict:
    response = await issue(http, actor, org, invoice_id, **body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def pay(
    http: AsyncClient,
    actor: Actor,
    org: dict,
    client_id: uuid.UUID | str,
    amount: int,
    *,
    received_on: date = date(2026, 10, 1),
    **body: Any,
) -> Response:
    return await http.post(
        org_url(org, "/payments"),
        headers=actor.headers,
        json={
            "client_id": str(client_id),
            "amount": amount,
            "received_on": received_on.isoformat(),
            **body,
        },
    )


async def get_invoice(http: AsyncClient, actor: Actor, org: dict, invoice_id: str) -> dict:
    response = await http.get(org_url(org, f"/invoices/{invoice_id}"), headers=actor.headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]
