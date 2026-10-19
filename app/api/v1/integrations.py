"""Integrations: the log of every synchronisation attempt. HTTP only."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.dependencies.common import SessionDep
from app.dependencies.integrations import SyncLogReader
from app.dependencies.organization import PageParamsDep
from app.repositories.integration_logs import SyncLogFilters
from app.schemas.common import ApiResponse, error_responses, ok_page
from app.schemas.integrations import SyncLogRead, SyncStatus
from app.services.integration_logs import SyncLogService

router = APIRouter(prefix="/organizations/{organization_id}", tags=["Integrations"])


def get_sync_log_service(session: SessionDep) -> SyncLogService:
    return SyncLogService(session)


SyncLogServiceDep = Annotated[SyncLogService, Depends(get_sync_log_service)]


@router.get(
    "/integrations/sync-logs",
    summary="List the integration sync log",
    description=(
        "Every synchronisation attempt of the practice's integrations, newest first, "
        "keyset-paginated: central client enable, link, sync, events and change-feed polls "
        "(`integration=central_client`), whether they worked (`ok`), failed or were skipped "
        "(an unknown client, a repeated event, nothing to do). Filter by `integration`, "
        "`status` and `entity_id` (the local record, e.g. a client id). Requires "
        "`integration.read` or `settings.manage`."
    ),
    responses=error_responses(403, 404),
)
async def list_sync_logs(
    org: SyncLogReader,
    service: SyncLogServiceDep,
    page: PageParamsDep,
    integration: Annotated[
        str | None,
        Query(
            max_length=40,
            pattern=r"^[a-z][a-z0-9_]{1,39}$",
            description="Only this integration, e.g. `central_client`",
        ),
    ] = None,
    status: Annotated[
        SyncStatus | None, Query(description="Only attempts with this outcome")
    ] = None,
    entity_id: Annotated[
        uuid.UUID | None, Query(description="Only attempts on this local record (a client id)")
    ] = None,
) -> ApiResponse[list[SyncLogRead]]:
    result = await service.list(
        org, SyncLogFilters(integration=integration, status=status, entity_id=entity_id), page
    )
    return ok_page([SyncLogRead.from_model(e) for e in result.items], result.next_cursor)
