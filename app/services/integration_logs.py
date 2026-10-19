"""The integration log: one row for every synchronisation attempt (docs/modules/central-clients.md).

``record_sync`` writes in the caller's transaction, so the row stands or falls with the change it
describes. ``record_failure`` writes in a transaction of its own, for an attempt whose own
transaction was rolled back.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import IntegrationSyncLog
from app.db.session import get_sessionmaker
from app.repositories.integration_logs import SyncLogFilters, SyncLogRepository
from app.services.policy import OrgContext
from app.utils.ids import new_id
from app.utils.pagination import Page, PageParams

log = get_logger(__name__)

CENTRAL_CLIENT = "central_client"
MAX_MESSAGE = 1000


async def record_sync(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    integration: str,
    operation: str,
    status: str,
    message: str | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    external_id: str | None = None,
    external_event_id: str | None = None,
    details: dict[str, Any] | None = None,
    direction: str = "inbound",
) -> IntegrationSyncLog:
    repo = SyncLogRepository(session)
    attempt = 1
    if status == "failed":
        attempt += await repo.failures_since_success(
            organization_id,
            integration,
            operation,
            entity_id=entity_id,
            external_id=external_id,
        )
    return await repo.add(
        IntegrationSyncLog(
            id=new_id(),
            organization_id=organization_id,
            integration=integration,
            direction=direction,
            operation=operation,
            entity_type=entity_type,
            entity_id=entity_id,
            external_id=external_id,
            external_event_id=external_event_id,
            status=status,
            message=message[:MAX_MESSAGE] if message else None,
            attempt=attempt,
            details=details,
        )
    )


async def record_failure(organization_id: uuid.UUID, **fields: Any) -> None:
    """Log a failed attempt in its own transaction; never raises (a failure to log a failure
    must not hide the failure)."""
    try:
        async with get_sessionmaker()() as session:
            await record_sync(session, organization_id, status="failed", **fields)
            await session.commit()
    except Exception as exc:
        log.error("sync_log_failed", error=type(exc).__name__)


class SyncLogService:
    def __init__(self, session: AsyncSession) -> None:
        self.logs = SyncLogRepository(session)

    async def list(
        self, ctx: OrgContext, filters: SyncLogFilters, params: PageParams
    ) -> Page[IntegrationSyncLog]:
        return await self.logs.list_page(ctx.organization_id, filters, params)
