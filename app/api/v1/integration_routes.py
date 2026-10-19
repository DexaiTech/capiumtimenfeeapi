"""Mounts the integration routers in the order their paths need.

The static paths (`/integrations/sync-items`, `/integrations/bookkeeping/mappings`, the central
user and practice management routes, `/integrations/subscription/mock`) must be matched before the
`/integrations/{key}` routes, so ``integration_cards`` comes last. ``api/router.py`` mounts this
after the sync-log router."""

from fastapi import APIRouter

from app.api.v1 import (
    bookkeeping_integration,
    central_user_integration,
    integration_cards,
    pm_integration,
    subscription,
)

router = APIRouter()
router.include_router(subscription.router)
router.include_router(bookkeeping_integration.router)
router.include_router(central_user_integration.router)
router.include_router(pm_integration.router)
router.include_router(integration_cards.router)
