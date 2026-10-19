"""Read-only counts and last outcomes behind the integration cards. Scoped to one organization."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, IntegrationSyncLog, Job, Task


@dataclass(frozen=True, slots=True)
class LogOutcome:
    """When the integration last worked and last tried, and why its last attempt failed (if it
    did): read from the sync log, which every attempt writes."""

    last_success_at: datetime | None
    last_attempt_at: datetime | None
    last_error: str | None


class IntegrationSummaryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def log_outcome(self, organization_id: uuid.UUID, integration: str) -> LogOutcome:
        log = IntegrationSyncLog
        stmt = (
            select(log.status, log.message, log.created_at)
            .where(log.organization_id == organization_id, log.integration == integration)
            .order_by(log.created_at.desc(), log.id.desc())
            .limit(50)
        )
        rows = (await self.session.execute(stmt)).all()
        meaningful = [row for row in rows if row[0] != "skipped"]
        last_ok = next((row[2] for row in meaningful if row[0] == "ok"), None)
        last = meaningful[0] if meaningful else None
        return LogOutcome(
            last_success_at=last_ok,
            last_attempt_at=last[2] if last else None,
            last_error=last[1] if last and last[0] == "failed" else None,
        )

    async def client_links(self, organization_id: uuid.UUID) -> dict[str, int]:
        """Clients linked to the central client service and how fresh they are."""
        stmt = select(
            func.count().filter(Client.central_client_id.is_not(None)),
            func.count().filter(Client.central_sync_state == "stale"),
            func.count().filter(Client.central_sync_state == "failed"),
        ).where(Client.organization_id == organization_id)
        linked, stale, failed = (await self.session.execute(stmt)).one()
        return {"linked_clients": int(linked), "stale_clients": int(stale), "failed_clients": int(failed)}

    async def pm_links(self, organization_id: uuid.UUID) -> dict[str, int]:
        """Jobs and tasks linked to practice management."""
        jobs = select(func.count()).where(
            Job.organization_id == organization_id,
            Job.pm_job_id.is_not(None),
            Job.deleted_at.is_(None),
        )
        tasks = select(func.count()).where(
            Task.organization_id == organization_id, Task.pm_task_id.is_not(None)
        )
        return {
            "linked_jobs": int((await self.session.execute(jobs)).scalar_one()),
            "linked_tasks": int((await self.session.execute(tasks)).scalar_one()),
        }
