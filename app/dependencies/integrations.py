"""Permission dependencies of the integration endpoints."""

from typing import Annotated

from fastapi import Depends

from app.core.exceptions import AuthorizationError
from app.dependencies.organization import OrgContextDep, require
from app.services.policy import OrgContext

IntegrationReader = Annotated[OrgContext, Depends(require("integration.read"))]
IntegrationManager = Annotated[OrgContext, Depends(require("integration.manage"))]


async def _sync_log_reader(ctx: OrgContextDep) -> OrgContext:
    """The sync log predates the integration permissions: it stays open to ``settings.manage``."""
    if not ({"integration.read", "settings.manage"} & ctx.permissions):
        raise AuthorizationError()
    return ctx


SyncLogReader = Annotated[OrgContext, Depends(_sync_log_reader)]
