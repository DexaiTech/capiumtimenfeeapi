"""Subscription: the status of the practice's subscription and the development control of the
mock subscription service. HTTP only; the check itself is ``app/services/subscription.py``.

These routes depend on the ungated membership (``MemberContextDep``): they must answer for a
practice whose subscription is not active, and the mock control is how a developer turns it back on.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.config import Settings
from app.core.exceptions import AuthorizationError, NotFoundError
from app.db.session import unit_of_work
from app.dependencies.common import ContextDep, SessionDep, SettingsDep
from app.dependencies.organization import MemberContextDep
from app.schemas.common import ApiResponse, error_responses, ok
from app.schemas.subscription import SubscriptionMock, SubscriptionMockUpdate, SubscriptionRead
from app.services import audit
from app.services.integrations import MockSubscriptionGateway, get_subscription_gateway
from app.services.policy import OrgContext
from app.services.subscription import current_state, invalidate_subscription

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Subscription"])


def mock_subscription_gateway(settings: Settings) -> MockSubscriptionGateway | None:
    """The mock subscription service while it is in use (never in production)."""
    gateway = get_subscription_gateway()
    if isinstance(gateway, MockSubscriptionGateway) and not settings.is_production:
        return gateway
    return None


def get_mock_gateway(settings: SettingsDep) -> MockSubscriptionGateway:
    gateway = mock_subscription_gateway(settings)
    if gateway is None:
        raise NotFoundError("Not found.")
    return gateway


async def integration_manager(ctx: MemberContextDep) -> OrgContext:
    if "integration.manage" not in ctx.permissions:
        raise AuthorizationError()
    return ctx


MockGatewayDep = Annotated[MockSubscriptionGateway, Depends(get_mock_gateway)]
MockManager = Annotated[OrgContext, Depends(integration_manager)]


def _mock_state(settings: Settings) -> SubscriptionMock | None:
    gateway = mock_subscription_gateway(settings)
    return SubscriptionMock(available=gateway.available) if gateway else None


@router.get(
    "/subscription",
    summary="Get the practice's subscription status",
    description=(
        "Whether the practice's subscription is `active`, `expired` or `suspended`, with the "
        "reason in words, when the subscription service last answered and whether this is its "
        "answer (`service`) or the last known one (`fallback`: it could not be asked). Any "
        "active member may read it, and it answers 200 even when the subscription is not "
        "active: every other practice route then answers 403 `SUBSCRIPTION_UNAVAILABLE` "
        "(`details.status`, `details.reason`). Answers are cached for 60 seconds; "
        "`refresh=true` asks the service again. 404 for a practice the caller is not a member of."
    ),
    responses=error_responses(404),
)
async def get_subscription(
    ctx: MemberContextDep,
    settings: SettingsDep,
    refresh: Annotated[
        bool, Query(description="Ask the subscription service again instead of the cached answer")
    ] = False,
) -> ApiResponse[SubscriptionRead]:
    state = await current_state(ctx.organization, refresh=refresh)
    return ok(SubscriptionRead.build(state, _mock_state(settings)))


@router.post(
    "/integrations/subscription/mock",
    summary="Set the mock subscription status",
    description=(
        "Development control: makes the mock subscription service report `active`, `expired` "
        "or `suspended` (with an optional reason) for this practice from now on, and optionally "
        "switches the whole mock service on or off (`available`: an outage). The answer cached "
        "for the practice is dropped at once. Like the status route it works while the "
        "subscription is not active, so the practice can be turned back on. 404 unless the "
        "gateway is the mock and `APP_ENV` is not production. Requires `integration.manage`."
    ),
    responses=error_responses(403, 404),
)
async def set_mock_subscription(
    body: SubscriptionMockUpdate,
    org: MockManager,
    gateway: MockGatewayDep,
    settings: SettingsDep,
    session: SessionDep,
    rctx: ContextDep,
) -> ApiResponse[SubscriptionRead]:
    previous = gateway.state_of(org.organization_id)
    gateway.set_status(org.organization_id, body.status, body.reason)
    if body.available is not None:
        gateway.set_available(body.available)
    invalidate_subscription(org.organization_id)
    async with unit_of_work(session):
        await audit.record(
            session,
            "subscription.mock_set",
            ctx=rctx,
            actor_user_id=org.user.id,
            organization_id=org.organization_id,
            target_type="organization",
            target_id=org.organization_id,
            meta={
                "changes": {"status": [previous.status, body.status]},
                "reason": body.reason,
                "available": body.available,
            },
        )
    state = await current_state(org.organization, refresh=True)
    return ok(SubscriptionRead.build(state, _mock_state(settings)), "Mock subscription set")
