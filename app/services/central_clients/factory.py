"""Chooses the central client gateway named by ``CENTRAL_CLIENT_GATEWAY``."""

from functools import lru_cache

from app.core.config import get_settings
from app.services.central_clients.base import CentralClientGateway
from app.services.central_clients.mock import MockCentralClientGateway


@lru_cache
def get_central_gateway() -> CentralClientGateway:
    """The process-wide gateway (the mock keeps its catalogue and its change feed in memory)."""
    match get_settings().central_client_gateway:
        case "mock":
            return MockCentralClientGateway()


def reset_central_gateway() -> None:
    """Forget the gateway so the next call builds a clean one (tests)."""
    get_central_gateway.cache_clear()
