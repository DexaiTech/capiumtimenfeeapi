"""Enqueue background jobs inside the caller's transaction."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import request_id_var
from app.db.models import BackgroundJob
from app.repositories.jobs import JobRepository
from app.utils.ids import new_id


async def enqueue(
    session: AsyncSession,
    job_type: str,
    payload: dict[str, Any] | None = None,
    *,
    run_at: datetime | None = None,
    dedupe_key: str | None = None,
    priority: int = 100,
    max_attempts: int = 5,
    organization_id: uuid.UUID | None = None,
) -> bool:
    """Returns False when an identical live job (same dedupe key) already exists."""
    body = dict(payload or {})
    request_id = request_id_var.get()
    if request_id:
        body.setdefault("request_id", request_id)
    job = BackgroundJob(
        id=new_id(),
        type=job_type,
        payload=body,
        dedupe_key=dedupe_key,
        priority=priority,
        max_attempts=max_attempts,
        organization_id=organization_id,
    )
    if run_at is not None:
        job.run_at = run_at
    return await JobRepository(session).enqueue(job)
