"""The central user integration: apply what the central user service says about the practice's staff
(renamed, deactivated, reactivated) and the mock service's change feed. HTTP only; the rules are in
``app/services/central_user_sync.py``."""

from datetime import UTC, datetime

from fastapi import APIRouter, status

from app.dependencies.common import ContextDep, SessionDep
from app.dependencies.integrations import IntegrationManager
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.integration_events import (
    CentralUserEventRequest,
    CentralUserMockChange,
    EventResult,
    MockChanged,
    ProcessedFeed,
)
from app.services.central_user_sync import CentralUserService
from app.services.integration_connections import ConnectionService
from app.services.integrations import (
    CENTRAL_USER,
    CentralUserEvent,
    MockCentralUserGateway,
    get_central_user_gateway,
)
from app.core.config import get_settings
from app.core.exceptions import NotFoundError

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Integrations"])


def _mock_gateway() -> MockCentralUserGateway:
    gateway = get_central_user_gateway()
    if not isinstance(gateway, MockCentralUserGateway) or get_settings().is_production:
        raise NotFoundError("Not found.")
    return gateway


@router.post(
    "/integrations/central-user/events",
    summary="Receive a central user event",
    description=(
        "One event from the central user service: `renamed` (with `full_name`), `deactivated` or "
        "`reactivated`, about the person with this `email`, who must be a member of the "
        "practice (else the event is `skipped`, never an error). A rename updates the person's "
        "name; a deactivation makes an active member `inactive` and ends their sessions when "
        "they belong to no other practice, but never the practice's last administrator (the "
        "event is then `failed`: it is kept as a failed sync item, retry it once someone else "
        "is an administrator); a reactivation returns an inactive member to active. Idempotent by "
        "`event_id`: a repeat is logged `skipped` and changes nothing. Handled as the system "
        "(no actor in the audit entry). 409 `INTEGRATION_DISABLED` while the integration is "
        "off. Requires `integration.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def receive_event(
    body: CentralUserEventRequest, org: IntegrationManager, session: SessionDep, ctx: ContextDep
) -> ApiResponse[EventResult]:
    await ConnectionService(session).ensure_enabled(org.organization_id, CENTRAL_USER)
    event = CentralUserEvent(
        event_id=body.event_id,
        type=body.type,
        central_user_id=body.central_user_id or "",
        email=str(body.email).lower(),
        occurred_at=body.occurred_at or datetime.now(UTC),
        full_name=body.full_name,
    )
    service = CentralUserService(session, get_central_user_gateway())
    handled = await service.handle(org, ctx, event, source="webhook")
    return ok(EventResult.from_handled(handled), "Event processed")


@router.post(
    "/integrations/central-user/process-changes",
    summary="Pull and apply the central user change feed",
    description=(
        "Reads the events after this practice's cursor from the central user service and applies "
        "each through the same handler as `POST /integrations/central-user/events` (at most 100 "
        "per call; `has_more` says to call again). The cursor moves forward only when the whole "
        "batch was handled; an event that a rule of the practice refuses is kept as a failed sync "
        "item and does not stop the feed. While the service is down the answer is 200 with "
        "`available: false` and the failure is logged. 409 `INTEGRATION_DISABLED` while the "
        "integration is off. Requires `integration.manage`."
    ),
    responses=error_responses(403, 404, 409),
)
async def process_changes(
    org: IntegrationManager, session: SessionDep, ctx: ContextDep
) -> ApiResponse[ProcessedFeed]:
    await ConnectionService(session).ensure_enabled(org.organization_id, CENTRAL_USER)
    service = CentralUserService(session, get_central_user_gateway())
    return ok(ProcessedFeed.from_polled(await service.process_changes(org, ctx)), "Changes processed")


@router.post(
    "/integrations/central-user/mock/changes",
    status_code=status.HTTP_201_CREATED,
    summary="Push a change into the mock central user service",
    description=(
        "Appends a `renamed`, `deactivated` or `reactivated` event about the person with this "
        "email to the mock central user service's change feed, which `process-changes` then reads. "
        "404 unless the gateway is the mock and `APP_ENV` is not production. Requires "
        "`integration.manage`."
    ),
    responses=error_responses(403, 404),
)
async def push_mock_change(
    body: CentralUserMockChange, org: IntegrationManager
) -> ApiResponse[MockChanged]:
    event = _mock_gateway().push(body.type, str(body.email), full_name=body.full_name)
    return ok(MockChanged(event_id=event.event_id, type=event.type), "Change pushed")
