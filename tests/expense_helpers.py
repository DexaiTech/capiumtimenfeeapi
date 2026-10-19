"""Helpers for the expense tests: claims and lines through the API, claims and lines inserted
straight into the tables (in any state), receipts and the practice's expense settings."""

import itertools
import uuid
from datetime import UTC, date, datetime
from typing import Any

from httpx import AsyncClient, Response

from app.db.models import Client, Expense, ExpenseClaim, Job
from app.db.session import get_sessionmaker
from tests.clients_helpers import sql
from tests.factories import Actor
from tests.ticket_helpers import SAMPLES, Team, new_team, org_url
from tests.time_helpers import err, ok_data

__all__ = [
    "LINE_DAY",
    "act",
    "add_line",
    "claim_body",
    "claim_url",
    "create_claim",
    "expense_team",
    "line_body",
    "line_row",
    "make_claim",
    "make_line",
    "patch_line",
    "post_receipt_multipart",
    "receipt_rows",
    "receipt_url",
    "type_id",
    "upload_receipt",
]

LINE_DAY = date(2026, 9, 7)
_counter = itertools.count(1)


async def expense_team(client: AsyncClient, name: str = "Acme Accountants") -> Team:
    """A practice with its Owner; ``team.member(role)`` adds a person with a system role."""
    return await new_team(client, name)


async def type_id(org: dict, code: str = "TRAVEL") -> uuid.UUID:
    """The id of one of the practice's expense types (the defaults are TRAVEL, MILEAGE,
    SUBSISTENCE, ACCOM, POSTAGE, PRINTING, SOFTWARE and OTHER)."""
    rows = await sql(
        "SELECT id FROM expense_types WHERE organization_id = :o AND code = :c",
        o=org["id"],
        c=code,
    )
    assert rows, f"no expense type {code}"
    return rows[0]["id"]


def claim_url(org: dict, claim_id: str | uuid.UUID | None = None, tail: str = "") -> str:
    base = org_url(org, "/expense-claims")
    return f"{base}/{claim_id}{tail}" if claim_id else f"{base}{tail}"


def receipt_url(
    org: dict, line_id: str | uuid.UUID, receipt_id: str | uuid.UUID | None = None
) -> str:
    base = org_url(org, f"/expenses/{line_id}/receipts")
    return f"{base}/{receipt_id}" if receipt_id else base


async def line_body(org: dict, code: str = "TRAVEL", **extra: Any) -> dict[str, Any]:
    """The request of a line: a train fare of 10.00 net, unless overridden (``None`` removes a
    key)."""
    body: dict[str, Any] = {
        "expense_type_id": str(await type_id(org, code)),
        "incurred_on": LINE_DAY.isoformat(),
        "description": "Train to the client",
        "net": 1_000,
        **extra,
    }
    return {key: value for key, value in body.items() if value is not None}


async def claim_body(org: dict, lines: int = 0, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = dict(extra)
    if lines:
        body["lines"] = [await line_body(org, description=f"Line {n}") for n in range(1, lines + 1)]
    return body


async def create_claim(
    client: AsyncClient, actor: Actor, org: dict, lines: int = 0, **extra: Any
) -> dict:
    """POST a claim through the API; returns ``data`` (the detail)."""
    response = await client.post(
        claim_url(org), headers=actor.headers, json=await claim_body(org, lines, **extra)
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def add_line(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    claim_id: str | uuid.UUID,
    code: str = "TRAVEL",
    status: int = 201,
    **extra: Any,
) -> dict:
    """POST a line; returns ``data`` for 201, else the error."""
    response = await client.post(
        claim_url(org, claim_id, "/lines"),
        headers=actor.headers,
        json=await line_body(org, code, **extra),
    )
    return ok_data(response, 201) if status == 201 else err(response, status)


async def patch_line(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    claim_id: str | uuid.UUID,
    line_id: str | uuid.UUID,
    status: int = 200,
    **body: Any,
) -> dict:
    response = await client.patch(
        claim_url(org, claim_id, f"/lines/{line_id}"), headers=actor.headers, json=body
    )
    return ok_data(response) if status == 200 else err(response, status)


async def act(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    claim_id: str | uuid.UUID,
    action: str,
    status: int = 200,
    json: dict | None = None,
) -> dict:
    """POST a workflow action (``submit``, ``recall``, ``approve``, ``return``)."""
    response = await client.post(
        claim_url(org, claim_id, f"/{action}"), headers=actor.headers, json=json
    )
    return ok_data(response) if status == 200 else err(response, status)


# --- Straight into the tables ---------------------------------------------------------------


async def make_claim(
    org: dict,
    user: Actor,
    *,
    status: str = "draft",
    title: str | None = None,
    created_by: Actor | None = None,
    decided_by: Actor | None = None,
    note: str | None = None,
    submitted_at: datetime | None = None,
) -> uuid.UUID:
    """A claim in any state, with the columns its status needs."""
    submitted = submitted_at or (
        datetime(2026, 9, 10, 9, 0, tzinfo=UTC) if status != "draft" else None
    )
    decided = (
        datetime(2026, 9, 11, 9, 0, tzinfo=UTC) if status in ("approved", "returned") else None
    )
    approver = (decided_by or user) if decided else None
    row = ExpenseClaim(
        organization_id=uuid.UUID(org["id"]),
        user_id=user.id,
        created_by=(created_by or user).id,
        title=title,
        status=status,
        submitted_at=submitted,
        decided_at=decided,
        decided_by=approver.id if approver else None,
        decision_note=(note or "Please check the dates") if status == "returned" else note,
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row.id


async def make_line(
    org: dict,
    claim_id: uuid.UUID,
    *,
    code: str = "TRAVEL",
    incurred_on: date = LINE_DAY,
    description: str | None = None,
    net: int = 1_000,
    vat: int = 0,
    client: Client | None = None,
    job: Job | None = None,
    is_billable: bool = False,
    recharge_amount: int = 0,
    billing_status: str = "unbilled",
    payer: str = "staff",
    **fields: Any,
) -> uuid.UUID:
    """A line with consistent money (``gross = net + vat``); billable needs a client."""
    row = Expense(
        organization_id=uuid.UUID(org["id"]),
        claim_id=claim_id,
        expense_type_id=await type_id(org, code),
        incurred_on=incurred_on,
        description=description or f"Expense {next(_counter)}",
        client_id=client.id if client else None,
        job_id=job.id if job else None,
        net=net,
        vat=vat,
        gross=net + vat,
        is_billable=is_billable,
        recharge_amount=recharge_amount,
        billing_status=billing_status,
        payer=payer,
        **fields,
    )
    async with get_sessionmaker()() as session:
        session.add(row)
        await session.commit()
        await session.refresh(row)
    return row.id


async def line_row(line_id: str | uuid.UUID) -> dict:
    rows = await sql("SELECT * FROM expenses WHERE id = :i", i=line_id)
    assert rows, f"no expense line {line_id}"
    return rows[0]


async def receipt_rows(line_id: str | uuid.UUID | None = None) -> list[dict]:
    where = "WHERE expense_id = :e" if line_id else ""
    params = {"e": line_id} if line_id else {}
    return await sql(
        f"SELECT * FROM expense_receipts {where} ORDER BY created_at, id",  # noqa: S608
        **params,
    )


async def upload_receipt(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    line_id: str | uuid.UUID,
    name: str = "receipt.png",
    data: bytes | None = None,
    content_type: str | None = "image/png",
) -> Response:
    """POST one file as ``multipart/form-data``."""
    payload = SAMPLES["png"].data if data is None else data
    return await client.post(
        receipt_url(org, line_id),
        headers=actor.headers,
        files={"file": (name, payload, content_type)},
    )


async def post_receipt_multipart(
    client: AsyncClient,
    actor: Actor,
    org: dict,
    line_id: str | uuid.UUID,
    body: bytes,
    content_type: str,
    **headers: str,
) -> Response:
    """POST a ready-made body to the receipt upload endpoint."""
    return await client.post(
        receipt_url(org, line_id),
        headers={**actor.headers, "Content-Type": content_type, **headers},
        content=body,
    )
