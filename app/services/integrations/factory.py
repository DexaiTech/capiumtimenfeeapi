"""Chooses the gateways named by ``CENTRAL_USER_GATEWAY``, ``PRACTICE_MANAGEMENT_GATEWAY``,
``BOOKKEEPING_GATEWAY`` and ``SUBSCRIPTION_GATEWAY`` (the central client gateway has its own
factory in ``app/services/central_clients``)."""

from functools import lru_cache

from app.core.config import get_settings
from app.services.integrations.bookkeeping import BookkeepingGateway, MockBookkeepingGateway
from app.services.integrations.central_user import CentralUserGateway, MockCentralUserGateway
from app.services.integrations.practice_management import (
    MockPracticeManagementGateway,
    PracticeManagementGateway,
)
from app.services.integrations.subscription import MockSubscriptionGateway, SubscriptionGateway


@lru_cache
def get_central_user_gateway() -> CentralUserGateway:
    """The process-wide gateway (the mock keeps its change feed in memory)."""
    match get_settings().central_user_gateway:
        case "mock":
            return MockCentralUserGateway()


@lru_cache
def get_practice_management_gateway() -> PracticeManagementGateway:
    match get_settings().practice_management_gateway:
        case "mock":
            return MockPracticeManagementGateway()


@lru_cache
def get_bookkeeping_gateway() -> BookkeepingGateway:
    match get_settings().bookkeeping_gateway:
        case "mock":
            return MockBookkeepingGateway()


@lru_cache
def get_subscription_gateway() -> SubscriptionGateway:
    match get_settings().subscription_gateway:
        case "mock":
            return MockSubscriptionGateway()


def reset_integration_gateways() -> None:
    """Forget the gateways so the next calls build clean ones (tests)."""
    get_central_user_gateway.cache_clear()
    get_practice_management_gateway.cache_clear()
    get_bookkeeping_gateway.cache_clear()
    get_subscription_gateway.cache_clear()
