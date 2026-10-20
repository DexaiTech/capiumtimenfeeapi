"""The integrations as a practice sees them: a card for each (status, last success, failures,
settings), "run sync now", the sync items (the outbox and its errors) with their retries, and the
switch of the mock services. HTTP only; the work is ``app/services/integration_*``."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.core.exceptions import NotFoundError
from app.db.session import unit_of_work
from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.integrations import IntegrationManager, IntegrationReader
from app.dependencies.organization import PageParamsDep
from app.repositories.integration_items import SyncItemFilters, SyncItemRepository
from app.schemas.common import ApiResponse, error_responses, ok, ok_page
from app.schemas.integration_connections import (
    IntegrationCard,
    IntegrationKey,
    IntegrationMockAvailabilityUpdate,
    IntegrationUpdate,
    ItemStatus,
    MockAvailabilityRead,
    RetryFailedResult,
    SyncItemRead,
    SyncItemRetried,
    SyncRunResult,
)
from app.services import audit
from app.services.integration_connections import ConnectionService, mock_for
from app.services.integration_outbox import MAX_ATTEMPTS
from app.services.integration_runner import IntegrationRunner

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Integrations"])

KeyPath = Annotated[
    IntegrationKey,
    Path(description=("`central_client`, `central_user`, `practice_management` or `bookkeeping`")),
]
ItemIdPath = Annotated[uuid.UUID, Path(description="The sync item's id")]


# --- Sync items (before the {key} routes: /integrations/sync-items is not a key) -------------


@router.get(
    "/integrations/sync-items",
    summary="List the sync items",
    description=(
        "The outbox and its error log, newest first, keyset-paginated: what is waiting to be sent "
        "or retried (`pending`), what was sent (`ok`), what failed and needs a person (`failed`) "
        "and what had nothing to do (`skipped`), with the attempts, the next attempt, the last "
        "error and the other system's reference. Filter by `integration`, `status`, "
        "`entity_type`, `entity_id` and `q` (the record's name, e.g. an invoice number, or the "
        "error). Requires `integration.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_sync_items(
    org: IntegrationReader,
    session: SessionDep,
    page: PageParamsDep,
    integration: Annotated[
        IntegrationKey | None, Query(description="Only this integration")
    ] = None,
    status: Annotated[ItemStatus | None, Query(description="Only items with this status")] = None,
    entity_type: Annotated[
        str | None,
        Query(
            max_length=30,
            pattern=r"^[a-z_]{1,30}$",
            description=(
                "Only this kind of record: `invoice`, `credit_note`, `job`, `task`, `member`"
            ),
        ),
    ] = None,
    entity_id: Annotated[
        uuid.UUID | None, Query(description="Only items about this record")
    ] = None,
    q: Annotated[
        str | None,
        Query(max_length=100, description="Part of the record's name or of the last error"),
    ] = None,
) -> ApiResponse[list[SyncItemRead]]:
    result = await SyncItemRepository(session).list_page(
        org.organization_id,
        SyncItemFilters(
            integration=integration,
            status=status,
            entity_type=entity_type,
            entity_id=entity_id,
            q=q.strip() if q and q.strip() else None,
        ),
        page,
    )
    return ok_page(
        [SyncItemRead.from_model(item, max_attempts=MAX_ATTEMPTS) for item in result.items],
        result.next_cursor,
    )


@router.post(
    "/integrations/sync-items/{item_id}/retry",
    summary="Retry a sync item now",
    description=(
        "Puts a `failed` (or waiting) item back to work and attempts it at once, with its "
        "attempts counted from zero, then says what happened: `sent` (now ok), `retry_scheduled` "
        "or `failed` (it failed again; the item says why), `skipped`, or `already_synced` for an "
        "item that was already ok (nothing is sent twice: the other system also books each "
        "idempotency key once). 409 `INTEGRATION_DISABLED` while the integration is switched "
        "off. Requires `integration.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def retry_sync_item(
    item_id: ItemIdPath, org: IntegrationManager, session: SessionDep, ctx: ContextDep
) -> ApiResponse[SyncItemRetried]:
    result = await IntegrationRunner(session, org, ctx).retry_item(item_id)
    return ok(result, "Sync item retried")


# --- Cards ---------------------------------------------------------------------------------


@router.get(
    "/integrations",
    summary="List the integrations",
    description=(
        "One card for each integration (central client, central user, practice management "
        "and bookkeeping): whether it is on, its status (`disabled`, `ready`, `connected` or "
        "`error`), when it last worked and why its last attempt failed, how many sync items "
        "are pending or failed, its settings with their defaults and counts specific to it "
        "(linked clients, linked jobs, documents sent). `mock` is present only while a mock "
        "service is in use. Requires `integration.read`."
    ),
    responses=error_responses(403, 404),
)
async def list_integrations(
    org: IntegrationReader, session: SessionDep
) -> ApiResponse[list[IntegrationCard]]:
    return ok(await ConnectionService(session).cards(org))


@router.patch(
    "/integrations/{key}",
    summary="Switch an integration on or off, or change its settings",
    description=(
        "`enabled` switches the integration on or off (the central client service is part of the "
        "client flow: 409 `INTEGRATION_ALWAYS_ON`); `config` changes settings, and those left "
        "out keep their value. Practice management: `sync_statuses`. Bookkeeping: "
        "`send_invoices`, `send_credit_notes` and `send_from` (only documents issued on or after "
        "that date are sent). 422 for an unknown or invalid setting. Every change is audited with "
        "the old and new values. Requires `integration.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def update_integration(
    key: KeyPath,
    body: IntegrationUpdate,
    org: IntegrationManager,
    session: SessionDep,
    ctx: ContextDep,
) -> ApiResponse[IntegrationCard]:
    return ok(await ConnectionService(session).update(org, key, body, ctx), "Integration updated")


@router.post(
    "/integrations/{key}/sync",
    summary="Run an integration now",
    description=(
        "Central client: reads the change feed and synchronises the linked clients. Central "
        "user: reads the change feed. Practice management: imports and links the jobs (and "
        "tasks), reads the change feed and pushes the statuses queued. Bookkeeping: queues the "
        "issued invoices and credit notes that have no sync item yet and sends what is due. "
        "Repeating it never duplicates anything. While the other system is down the answer is "
        "still 200 with `available: false` and the work stays queued; 409 `INTEGRATION_DISABLED` "
        "while the integration is switched off. Requires `integration.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def run_integration(
    key: KeyPath, org: IntegrationManager, session: SessionDep, ctx: ContextDep
) -> ApiResponse[SyncRunResult]:
    return ok(await IntegrationRunner(session, org, ctx).run_now(key), "Synchronisation run")


@router.post(
    "/integrations/{key}/retry-failed",
    summary="Retry every failed sync item of an integration",
    description=(
        "Puts the integration's `failed` items back to work and attempts each one now (at most "
        "200 per call; `remaining` counts the rest): the way to resend everything after a "
        "missing mapping was added. Items that are already ok are never sent again. 409 "
        "`INTEGRATION_DISABLED` while the integration is switched off. Requires "
        "`integration.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def retry_failed_items(
    key: KeyPath, org: IntegrationManager, session: SessionDep, ctx: ContextDep
) -> ApiResponse[RetryFailedResult]:
    return ok(await IntegrationRunner(session, org, ctx).retry_failed(key), "Failed items retried")


@router.post(
    "/integrations/{key}/mock/availability",
    summary="Switch a mock service on or off",
    description=(
        "Development control: makes the mock service of the integration answer as if it were "
        "down (`available: false`) or up again, so the retries and the failed items can be "
        "shown. The switch is process-wide. 404 unless the gateway is the mock and `APP_ENV` is "
        "not production. Requires `integration.manage`."
    ),
    responses=error_responses(403, 404),
)
async def set_mock_availability(
    key: KeyPath,
    body: IntegrationMockAvailabilityUpdate,
    org: IntegrationManager,
    session: SessionDep,
    ctx: ContextDep,
) -> ApiResponse[MockAvailabilityRead]:
    mock = mock_for(key)
    if mock is None:
        raise NotFoundError("Not found.")
    previous = mock.available
    mock.set_available(body.available)
    async with unit_of_work(session):
        await audit.record(
            session,
            "integration.mock_availability_set",
            ctx=ctx,
            actor_user_id=org.user.id,
            organization_id=org.organization_id,
            target_type="integration",
            meta={"integration": key, "changes": {"available": [previous, body.available]}},
        )
    message = "Mock service is on" if body.available else "Mock service is off"
    return ok(MockAvailabilityRead(integration=key, available=mock.available), message)
