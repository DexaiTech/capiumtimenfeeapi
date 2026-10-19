"""The central client integration: search and enable central clients, link and synchronise the
practice's clients, receive central events, and the mock central service's switches. HTTP only."""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response, status

from app.core.exceptions import NotFoundError
from app.dependencies.common import ContextDep, SessionDep, SettingsDep
from app.dependencies.organization import require
from app.schemas.central_clients import (
    CENTRAL_ID_PATTERN,
    CentralEnabled,
    CentralEventRequest,
    CentralEventResult,
    CentralLinkRequest,
    CentralSearchResult,
    CentralSyncAll,
    ClientSynced,
    MockAvailability,
    MockAvailabilityUpdate,
    MockChanged,
    MockChangeRequest,
    ProcessedChanges,
)
from app.schemas.clients import ClientDetail
from app.schemas.common import ApiResponse, error_responses, ok
from app.services.central_clients import (
    CentralClientEvent,
    CentralClientGateway,
    MockCentralClientGateway,
    get_central_gateway,
)
from app.services.central_events import CentralEventService
from app.services.central_sync import CentralClientService
from app.services.policy import OrgContext

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Central clients"])

ClientCreator = Annotated[OrgContext, Depends(require("client.create"))]
ClientEditor = Annotated[OrgContext, Depends(require("client.update"))]
SettingsManager = Annotated[OrgContext, Depends(require("settings.manage"))]
GatewayDep = Annotated[CentralClientGateway, Depends(get_central_gateway)]
CentralIdPath = Annotated[
    str,
    Path(
        min_length=1,
        max_length=64,
        pattern=CENTRAL_ID_PATTERN,
        description="The client's id at the central client service",
    ),
]


def get_central_service(session: SessionDep, gateway: GatewayDep) -> CentralClientService:
    return CentralClientService(session, gateway)


def get_event_service(session: SessionDep, gateway: GatewayDep) -> CentralEventService:
    return CentralEventService(session, gateway)


def get_mock_gateway(gateway: GatewayDep, settings: SettingsDep) -> MockCentralClientGateway:
    """The mock central service, for the endpoints that only exist while it is in use."""
    if not isinstance(gateway, MockCentralClientGateway) or settings.is_production:
        raise NotFoundError("Not found.")
    return gateway


CentralServiceDep = Annotated[CentralClientService, Depends(get_central_service)]
EventServiceDep = Annotated[CentralEventService, Depends(get_event_service)]
MockGatewayDep = Annotated[MockCentralClientGateway, Depends(get_mock_gateway)]
ClientIdPath = Annotated[uuid.UUID, Path(description="The client's id")]


# --- Search and enable ------------------------------------------------------------------


@router.get(
    "/central-clients",
    summary="Search the central client service",
    description=(
        "Central clients whose name or company number contains `q` (blank: the first `limit` "
        "by name). Each item carries the central fields, `enabled` and `client_id` (a live "
        "client of the practice is linked to it) and `matching_client` (a client that is not "
        "linked but has the same company number: enabling links it instead of creating a "
        "second one). `source` is `live`, and every live answer refreshes the practice's cache; "
        "while the central service is down it is `cache` with `stale: true`, and 503 "
        "`CENTRAL_UNAVAILABLE` when nothing is cached yet. Requires `client.create`."
    ),
    responses=error_responses(403, 404, 503),
)
async def search_central_clients(
    org: ClientCreator,
    service: CentralServiceDep,
    q: Annotated[
        str,
        Query(max_length=200, description="Part of the name or the company number; blank lists"),
    ] = "",
    limit: Annotated[int, Query(ge=1, le=50, description="Most results to return")] = 20,
) -> ApiResponse[CentralSearchResult]:
    return ok(await service.search(org, q.strip(), limit))


@router.post(
    "/central-clients/{central_id}/enable",
    summary="Enable a central client",
    description=(
        "Creates the practice's client for a central client (code from the client sequence; "
        "name, address and company number from the central service; status `active`) linked "
        "through `central_client_id`: 201, `result: created`. Never a duplicate: a central "
        "client that is already enabled answers 200 with the existing client "
        "(`already_enabled`), and a client of the practice that is not linked and has the "
        "same company number (ignoring spaces, case and leading zeros) is linked and takes "
        "the central name and address (200, `linked_existing`). While the central service is "
        "down the cached copy is used (`from_cache: true`, the client is marked `stale`). "
        "404 for an id the central service does not know, 409 `CENTRAL_CLIENT_ARCHIVED` for "
        "a client that is archived centrally, 503 `CENTRAL_UNAVAILABLE` when it is down and "
        "the client is not cached. Requires `client.create`."
    ),
    responses={
        **error_responses(403, 404, 409, 503),
        201: {"description": "A client was created"},
    },
)
async def enable_central_client(
    central_id: CentralIdPath,
    org: ClientCreator,
    service: CentralServiceDep,
    ctx: ContextDep,
    response: Response,
) -> ApiResponse[CentralEnabled]:
    enabled, created = await service.enable(org, central_id, ctx)
    if created:
        response.status_code = status.HTTP_201_CREATED
    messages = {
        "created": "Client created from the central client service",
        "linked_existing": "Existing client linked to the central client service",
        "already_enabled": "This central client is already enabled",
    }
    return ok(enabled, messages[enabled.result])


# --- Linking and synchronising clients --------------------------------------------------


@router.post(
    "/clients/central-sync",
    summary="Synchronise every linked client",
    description=(
        "Pulls the name and address of the practice's linked clients that are not archived "
        "(at most 200 per call, the longest unsynced first; `remaining` counts the rest) and "
        "reports each client's `changes` as `{field: [old, new]}`. While the central service "
        "is down the stored values stay, the clients become `stale` and the failures are "
        "logged: the answer is still 200 with `available: false`. Requires `client.update`."
    ),
    responses=error_responses(403, 404),
)
async def sync_central_clients(
    org: ClientEditor, service: CentralServiceDep, ctx: ContextDep
) -> ApiResponse[CentralSyncAll]:
    return ok(await service.sync_all(org, ctx), "Clients synchronised")


@router.post(
    "/clients/{client_id}/central-sync",
    summary="Synchronise a client with the central client service",
    description=(
        "Pulls the client's name and address from the central service (`changes` lists what "
        "changed). While the service is down the stored values stay, `state` becomes `stale` "
        "and the failure is logged: still 200. `failed` means the central service no longer "
        "knows the client. 409 `CLIENT_NOT_LINKED` for a client that is not linked and 409 "
        "`CLIENT_MERGED` for one that was merged away. Requires `client.update`."
    ),
    responses=error_responses(403, 404, 409),
)
async def sync_central_client(
    client_id: ClientIdPath, org: ClientEditor, service: CentralServiceDep, ctx: ContextDep
) -> ApiResponse[ClientSynced]:
    return ok(await service.sync_client(org, client_id, ctx), "Client synchronised")


@router.post(
    "/clients/{client_id}/central-link",
    summary="Link a client to a central client",
    description=(
        "Links a client of the practice to a central client: it takes the central name and "
        "address (which are then managed centrally) and fills a missing company number. 409 "
        "`CENTRAL_CLIENT_ALREADY_LINKED` when another client already uses that central id, "
        "409 `CLIENT_ALREADY_LINKED` when this client is linked (unlink it first), 409 "
        "`CENTRAL_CLIENT_ARCHIVED`, 404 for an unknown client or central id, 503 "
        "`CENTRAL_UNAVAILABLE` when the service is down and the client is not cached. "
        "Requires `client.create`."
    ),
    responses=error_responses(403, 404, 409, 503),
)
async def link_central_client(
    client_id: ClientIdPath,
    body: CentralLinkRequest,
    org: ClientCreator,
    service: CentralServiceDep,
    ctx: ContextDep,
) -> ApiResponse[ClientDetail]:
    return ok(await service.link(org, client_id, body.central_id, ctx), "Client linked")


@router.delete(
    "/clients/{client_id}/central-link",
    summary="Unlink a client from the central client service",
    description=(
        "Makes the client a local client again: the link is removed and its name and address "
        "can be edited. 409 `CLIENT_NOT_LINKED` for a client that is not linked. "
        "Requires `client.create`."
    ),
    responses=error_responses(403, 404, 409),
)
async def unlink_central_client(
    client_id: ClientIdPath, org: ClientCreator, service: CentralServiceDep, ctx: ContextDep
) -> ApiResponse[ClientDetail]:
    return ok(await service.unlink(org, client_id, ctx), "Client unlinked")


# --- Central events ---------------------------------------------------------------------


@router.post(
    "/central-clients/events",
    summary="Receive a central client event",
    description=(
        "One event from the central client service: `renamed` (new `name` and/or address "
        'fields; omit a field to leave it, send `""` to clear an address field), `archived`, '
        "`restored` or `merged` (`central_id` was merged into `into_central_id`). It is "
        "applied to the linked client and the practice's cache by the same handler as "
        "`process-changes`. Idempotent by `event_id`: an event delivered again is logged "
        "`skipped` and changes nothing (200). An event for a central client that is not "
        "enabled here is `skipped`, never an error. A merge moves everything of the merged "
        "client to the survivor in one transaction (see the central clients module). "
        "Requires `settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def receive_central_event(
    body: CentralEventRequest, org: SettingsManager, service: EventServiceDep, ctx: ContextDep
) -> ApiResponse[CentralEventResult]:
    event = CentralClientEvent(
        event_id=body.event_id,
        type=body.type,
        central_id=body.central_id,
        occurred_at=body.occurred_at or datetime.now(UTC),
        name=body.name,
        address_line1=body.address_line1,
        address_line2=body.address_line2,
        city=body.city,
        postcode=body.postcode,
        country=body.country,
        into_central_id=body.into_central_id,
    )
    result = await service.handle(
        org.organization_id, event, source="webhook", rctx=ctx, requested_by=org.user.id
    )
    return ok(result, "Event processed")


@router.post(
    "/central-clients/process-changes",
    summary="Pull and apply the central change feed",
    description=(
        "Reads the events after this practice's cursor from the central service and applies "
        "each through the same handler as `POST /central-clients/events` (at most 100 per "
        "call; `has_more` says to call again). The cursor moves forward only when the whole "
        "batch was processed; a repeated event is harmless. While the central service is "
        "down the answer is 200 with `available: false` and the failure is logged. "
        "Requires `settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def process_central_changes(
    org: SettingsManager, service: EventServiceDep, ctx: ContextDep
) -> ApiResponse[ProcessedChanges]:
    return ok(await service.process_changes(org, ctx), "Central changes processed")


# --- The mock central service -----------------------------------------------------------


@router.get(
    "/central-clients/mock/availability",
    summary="Is the mock central service on?",
    description=(
        "Whether the mock central client service answers. 404 unless the gateway is the "
        "mock (`CENTRAL_CLIENT_GATEWAY=mock`) and `APP_ENV` is not production. "
        "Requires `settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def get_mock_availability(
    org: SettingsManager, gateway: MockGatewayDep
) -> ApiResponse[MockAvailability]:
    return ok(MockAvailability(available=gateway.available, gateway=gateway.name))


@router.post(
    "/central-clients/mock/availability",
    summary="Switch the mock central service on or off",
    description=(
        "Makes the mock central client service answer as if it were down (`available: "
        "false`) or up again, so the cached-data behaviour can be shown: searches and "
        "enabling use the cache, synchronisations mark clients stale. The switch is "
        "process-wide. 404 unless the gateway is the mock and `APP_ENV` is not production. "
        "Requires `settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def set_mock_availability(
    body: MockAvailabilityUpdate, org: SettingsManager, gateway: MockGatewayDep
) -> ApiResponse[MockAvailability]:
    gateway.set_available(body.available)
    message = "Mock central service is on" if body.available else "Mock central service is off"
    return ok(MockAvailability(available=gateway.available, gateway=gateway.name), message)


@router.post(
    "/central-clients/mock/changes",
    status_code=status.HTTP_201_CREATED,
    summary="Push a change into the mock central service",
    description=(
        "Renames, archives, restores or merges a client of the mock central service and "
        "appends the event to its change feed, which `process-changes` then reads. 404 for "
        "an unknown central id, and unless the gateway is the mock and `APP_ENV` is not "
        "production. Requires `settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def push_mock_change(
    body: MockChangeRequest, org: SettingsManager, gateway: MockGatewayDep
) -> ApiResponse[MockChanged]:
    try:
        event = gateway.push(
            body.type,
            body.central_id,
            name=body.name,
            address_line1=body.address_line1,
            address_line2=body.address_line2,
            city=body.city,
            postcode=body.postcode,
            country=body.country,
            into_central_id=body.into_central_id,
        )
    except KeyError as exc:
        raise NotFoundError("Central client not found.") from exc
    return ok(
        MockChanged(event_id=event.event_id, type=event.type, central_id=event.central_id),
        "Change pushed",
    )
