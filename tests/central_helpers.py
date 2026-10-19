"""Helpers for the central client, client merge, billing settings, invoice template and client
overview tests. The central service is the mock gateway; the ``central_gateway`` fixture
(``tests/conftest.py``) gives every test a clean one."""

import itertools
import uuid
from typing import Any

from httpx import AsyncClient, Response

from tests.clients_helpers import org_url, scalar, sql
from tests.factories import Actor
from tests.ticket_helpers import Team, new_team

__all__ = [
    "ARCHIVED",
    "ASHBY",
    "BAKERY",
    "BOOKKEEPER",
    "JOINERY",
    "KESTREL",
    "PLUMBING",
    "Team",
    "client_row",
    "enable",
    "enabled",
    "link",
    "new_team",
    "process_changes",
    "scalar",
    "search",
    "send_event",
    "sql",
    "sync_all",
    "sync_client",
    "sync_logs",
    "unlink",
]

# Clients of the mock central service (app/services/central_clients/mock.py).
BAKERY = "CC-0001"  # Northgate Bakery Ltd, 09231457, 12 Northgate Street, Leeds LS1 4AB
PLUMBING = "CC-0002"  # Brightwater Plumbing & Heating Ltd, 08345129, Nottingham
JOINERY = "CC-0003"  # Calder Valley Joinery Ltd, 07765432, Halifax
ASHBY = "CC-0004"  # Ashby & Partners LLP, OC412345, Derby
KESTREL = "CC-0005"  # Kestrel Digital Marketing Ltd, 11456789, Manchester
BOOKKEEPER = "CC-0009"  # Sandra Whitfield t/a Whitfield Bookkeeping: no company number
ARCHIVED = "CC-0023"  # Old Mill Printers Ltd: archived centrally

_event_numbers = itertools.count(1)


def next_event_id(prefix: str = "evt") -> str:
    return f"{prefix}-{next(_event_numbers):05d}"


async def search(http: AsyncClient, actor: Actor, org: dict, **params: Any) -> Response:
    clean = {k: v for k, v in params.items() if v is not None}
    return await http.get(org_url(org, "/central-clients"), headers=actor.headers, params=clean)


async def enable(http: AsyncClient, actor: Actor, org: dict, central_id: str) -> Response:
    return await http.post(
        org_url(org, f"/central-clients/{central_id}/enable"), headers=actor.headers
    )


async def enabled(http: AsyncClient, actor: Actor, org: dict, central_id: str) -> dict:
    """Enable a central client that is not enabled yet; returns ``data`` (the 201 answer)."""
    response = await enable(http, actor, org, central_id)
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def link(
    http: AsyncClient, actor: Actor, org: dict, client_id: str | uuid.UUID, central_id: str
) -> Response:
    return await http.post(
        org_url(org, f"/clients/{client_id}/central-link"),
        headers=actor.headers,
        json={"central_id": central_id},
    )


async def unlink(
    http: AsyncClient, actor: Actor, org: dict, client_id: str | uuid.UUID
) -> Response:
    return await http.delete(
        org_url(org, f"/clients/{client_id}/central-link"), headers=actor.headers
    )


async def sync_client(
    http: AsyncClient, actor: Actor, org: dict, client_id: str | uuid.UUID
) -> Response:
    return await http.post(
        org_url(org, f"/clients/{client_id}/central-sync"), headers=actor.headers
    )


async def sync_all(http: AsyncClient, actor: Actor, org: dict) -> Response:
    return await http.post(org_url(org, "/clients/central-sync"), headers=actor.headers)


async def send_event(
    http: AsyncClient,
    actor: Actor,
    org: dict,
    type_: str,
    central_id: str,
    *,
    event_id: str | None = None,
    **fields: Any,
) -> Response:
    """Deliver one central event (``POST /central-clients/events``)."""
    return await http.post(
        org_url(org, "/central-clients/events"),
        headers=actor.headers,
        json={
            "event_id": event_id or next_event_id(),
            "type": type_,
            "central_id": central_id,
            **fields,
        },
    )


async def process_changes(http: AsyncClient, actor: Actor, org: dict) -> Response:
    return await http.post(org_url(org, "/central-clients/process-changes"), headers=actor.headers)


async def sync_logs(http: AsyncClient, actor: Actor, org: dict, **params: Any) -> list[dict]:
    """The sync-log rows (newest first) for the filters; asserts 200."""
    clean = {k: str(v) for k, v in params.items() if v is not None}
    response = await http.get(
        org_url(org, "/integrations/sync-logs"), headers=actor.headers, params=clean
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def client_row(client_id: str | uuid.UUID) -> dict:
    """The raw ``clients`` row."""
    rows = await sql("SELECT * FROM clients WHERE id = :i", i=str(client_id))
    assert len(rows) == 1, f"no client {client_id}"
    return rows[0]
