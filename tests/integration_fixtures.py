"""The autouse fixture behind the integration tests, imported by ``tests/conftest.py``: every test
starts with clean mock gateways (central user, practice management, bookkeeping, subscription) and an
empty subscription cache, so a practice made ``expired`` or a switched-off mock never leaks into the
next test. Kept out of ``conftest`` so the shared file only gains one import."""

from collections.abc import Iterator

import pytest

from app.services.integrations import reset_integration_gateways
from app.services.subscription import reset_subscription_cache


@pytest.fixture(autouse=True)
def integration_gateways() -> Iterator[None]:
    reset_integration_gateways()
    reset_subscription_cache()
    yield
    reset_integration_gateways()
    reset_subscription_cache()
