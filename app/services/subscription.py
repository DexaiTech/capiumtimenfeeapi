"""Subscription verification: a practice may use the product only while its subscription is active.

``get_org_context`` (the one place that resolves a practice for every practice route) calls
:func:`ensure_subscription_active` after it has proved the caller is a member, so another
practice's id still answers 404 and only a member sees 403 ``SUBSCRIPTION_UNAVAILABLE``.

The answer of the subscription service is cached per practice for ``SUBSCRIPTION_CACHE_SECONDS``
(60 by default), so a request costs a dictionary lookup, never a call, and no database query.
When the service cannot be asked, the last answer is kept (an expired practice stays blocked, an
active one stays open); a practice that was never verified is let through, because an outage of
the subscription service must not lock every practice out, and the next attempt is made after
``FAILURE_TTL_SECONDS``.
"""

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

from app.core.config import get_settings
from app.core.exceptions import AuthorizationError
from app.core.logging import get_logger
from app.db.models import Organization
from app.services.integrations import SubscriptionStatus, get_subscription_gateway

log = get_logger(__name__)

FAILURE_TTL_SECONDS = 10.0
MAX_CACHED_PRACTICES = 20_000
# ``service``: the subscription service said so (possibly a minute ago); ``fallback``: it could
# not be asked and this is the last known (or assumed) status.
Source = Literal["service", "fallback"]


@dataclass(frozen=True, slots=True)
class SubscriptionState:
    status: SubscriptionStatus
    reason: str | None
    checked_at: datetime
    source: Source

    @property
    def active(self) -> bool:
        return self.status == "active"


class SubscriptionUnavailableError(AuthorizationError):
    """403 for every practice route while the subscription is not active (``details.status`` and
    ``details.reason`` say why)."""

    code = "SUBSCRIPTION_UNAVAILABLE"
    message = "Your practice's subscription is not available."

    def __init__(self, state: SubscriptionState) -> None:
        super().__init__(details={"status": state.status, "reason": state.reason})


class SubscriptionCache:
    """Per-practice answers with an expiry; an expired entry stays as the last known answer."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_CACHED_PRACTICES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._items: dict[uuid.UUID, tuple[float, SubscriptionState]] = {}
        self._max_entries = max_entries
        self._clock = clock

    def fresh(self, organization_id: uuid.UUID) -> SubscriptionState | None:
        entry = self._items.get(organization_id)
        if entry is not None and entry[0] > self._clock():
            return entry[1]
        return None

    def last_known(self, organization_id: uuid.UUID) -> SubscriptionState | None:
        entry = self._items.get(organization_id)
        return entry[1] if entry is not None else None

    def put(self, organization_id: uuid.UUID, state: SubscriptionState, ttl: float) -> None:
        self._items.pop(organization_id, None)
        while len(self._items) >= self._max_entries:
            del self._items[next(iter(self._items))]
        self._items[organization_id] = (self._clock() + ttl, state)

    def invalidate(self, organization_id: uuid.UUID | None = None) -> None:
        if organization_id is None:
            self._items.clear()
        else:
            self._items.pop(organization_id, None)

    def __len__(self) -> int:
        return len(self._items)


_cache = SubscriptionCache()


def reset_subscription_cache() -> None:
    """Forget every cached answer (tests, and a status that was just changed)."""
    _cache.invalidate()


def invalidate_subscription(organization_id: uuid.UUID) -> None:
    _cache.invalidate(organization_id)


async def current_state(organization: Organization, *, refresh: bool = False) -> SubscriptionState:
    """The subscription state of a practice: the cached answer, or a fresh one (``refresh``)."""
    if not refresh:
        cached = _cache.fresh(organization.id)
        if cached is not None:
            return cached
    now = datetime.now(UTC)
    try:
        info = await get_subscription_gateway().status(organization)
    except Exception as exc:
        known = _cache.last_known(organization.id)
        state = (
            replace(known, source="fallback")
            if known is not None
            else SubscriptionState("active", None, now, "fallback")
        )
        _cache.put(organization.id, state, FAILURE_TTL_SECONDS)
        log.warning(
            "subscription_check_failed",
            organization_id=str(organization.id),
            error=type(exc).__name__,
            status=state.status,
        )
        return state
    state = SubscriptionState(info.status, info.reason, now, "service")
    _cache.put(organization.id, state, float(get_settings().subscription_cache_seconds))
    return state


async def ensure_subscription_active(organization: Organization) -> None:
    """Raise 403 ``SUBSCRIPTION_UNAVAILABLE`` unless the practice's subscription is active."""
    state = _cache.fresh(organization.id) or await current_state(organization)
    if state.status != "active":
        raise SubscriptionUnavailableError(state)
