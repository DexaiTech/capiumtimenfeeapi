"""The subscription service behind one interface.

A practice may use Time & Fees only while its subscription is active. The central subscription
service answers ``active``, ``expired`` or ``suspended`` (with a reason in words) for a practice;
the API asks it through :class:`SubscriptionGateway` (cached, see ``app/services/subscription.py``)
and refuses every practice route of a practice that is not active. The real service is one more
implementation next to :class:`MockSubscriptionGateway`.
"""

import uuid
from dataclasses import dataclass
from typing import Literal, Protocol

from app.db.models import Organization
from app.services.integrations.common import IntegrationUnavailableError

SubscriptionStatus = Literal["active", "expired", "suspended"]
SUBSCRIPTION_STATUSES: tuple[SubscriptionStatus, ...] = ("active", "expired", "suspended")
DEFAULT_REASONS: dict[SubscriptionStatus, str | None] = {
    "active": None,
    "expired": "The practice's subscription has expired.",
    "suspended": "The practice's subscription has been suspended.",
}


@dataclass(frozen=True, slots=True)
class SubscriptionInfo:
    status: SubscriptionStatus
    # Why the practice cannot use the product, in words for the person who sees it.
    reason: str | None = None


class SubscriptionGateway(Protocol):
    """A subscription service. ``status`` raises :class:`IntegrationUnavailableError` when the
    service cannot be used."""

    name: str

    async def status(self, organization: Organization) -> SubscriptionInfo: ...


class MockSubscriptionGateway:
    """In-memory subscriptions for development and tests: every practice is active until it is
    set otherwise, and the service can be switched off. It counts its calls, which is how the
    tests prove the cache. State lives in the process."""

    name = "mock"

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Every practice active, switched on, no calls counted."""
        self._states: dict[uuid.UUID, SubscriptionInfo] = {}
        self._available = True
        self.calls = 0

    @property
    def available(self) -> bool:
        return self._available

    def set_available(self, available: bool) -> None:
        self._available = available

    def set_status(
        self,
        organization_id: uuid.UUID,
        status: SubscriptionStatus,
        reason: str | None = None,
    ) -> SubscriptionInfo:
        info = SubscriptionInfo(status, reason or DEFAULT_REASONS[status])
        if status == "active":
            self._states.pop(organization_id, None)
        else:
            self._states[organization_id] = info
        return info

    def state_of(self, organization_id: uuid.UUID) -> SubscriptionInfo:
        return self._states.get(organization_id, SubscriptionInfo("active"))

    async def status(self, organization: Organization) -> SubscriptionInfo:
        self.calls += 1
        if not self._available:
            raise IntegrationUnavailableError("The mock subscription service is switched off.")
        return self.state_of(organization.id)
