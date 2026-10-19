import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BackgroundJob

_CLAIM = text(
    """
    UPDATE background_jobs
    SET status = 'running', attempts = attempts + 1,
        locked_by = :worker_id, locked_until = now() + make_interval(secs => :lease)
    WHERE id IN (
        SELECT id FROM background_jobs
        WHERE status = 'queued' AND run_at <= now()
        ORDER BY priority, run_at
        FOR UPDATE SKIP LOCKED
        LIMIT :batch
    )
    RETURNING id
    """
)


@dataclass(frozen=True, slots=True)
class ReapedJob:
    """A job whose lease expired for the last time."""

    id: uuid.UUID
    organization_id: uuid.UUID | None
    type: str
    attempts: int


@dataclass(frozen=True, slots=True)
class ReapResult:
    requeued: int
    dead: list[ReapedJob]

    @property
    def total(self) -> int:
        return self.requeued + len(self.dead)


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(self, job: BackgroundJob) -> bool:
        """Insert a job. Returns False when a live job with the same dedupe key exists."""
        values: dict[str, Any] = {
            "id": job.id,
            "organization_id": job.organization_id,
            "type": job.type,
            "payload": job.payload,
            "priority": job.priority,
            "max_attempts": job.max_attempts,
            "dedupe_key": job.dedupe_key,
        }
        if job.run_at is not None:
            values["run_at"] = job.run_at
        stmt = insert(BackgroundJob).values(**values).on_conflict_do_nothing()
        result: CursorResult[tuple[()]] = await self.session.execute(stmt)  # type: ignore[assignment]
        return result.rowcount > 0

    async def existing_dedupe_keys(self, keys: list[str]) -> set[str]:
        """Which of ``keys`` belong to a job in any state, finished ones included."""
        if not keys:
            return set()
        stmt = select(BackgroundJob.dedupe_key).where(BackgroundJob.dedupe_key.in_(keys))
        return {key for key in (await self.session.execute(stmt)).scalars() if key}

    async def claim(self, worker_id: str, batch: int, lease_seconds: int) -> list[BackgroundJob]:
        rows = await self.session.execute(
            _CLAIM, {"worker_id": worker_id, "batch": batch, "lease": lease_seconds}
        )
        ids = [row[0] for row in rows]
        if not ids:
            return []
        result = await self.session.execute(select(BackgroundJob).where(BackgroundJob.id.in_(ids)))
        return list(result.scalars())

    async def finish(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        *,
        status: str,
        payload: dict[str, Any] | None = None,
        last_error: str | None = None,
        run_at: datetime | None = None,
    ) -> bool:
        """Record the outcome. A worker that lost its lease cannot overwrite the new owner."""
        values: dict[str, Any] = {
            "status": status,
            "locked_by": None,
            "locked_until": None,
            "last_error": last_error,
        }
        if status == "queued":
            values["run_at"] = run_at
        else:
            values["finished_at"] = func.now()
        if payload is not None:
            values["payload"] = payload
        stmt = (
            update(BackgroundJob)
            .where(
                BackgroundJob.id == job_id,
                BackgroundJob.locked_by == worker_id,
                BackgroundJob.status == "running",
            )
            .values(**values)
        )
        result: CursorResult[tuple[()]] = await self.session.execute(stmt)  # type: ignore[assignment]
        return result.rowcount > 0

    async def reap_expired(self) -> ReapResult:
        """Return jobs whose lease expired to the queue, or mark them dead."""
        expired = BackgroundJob.status == "running", BackgroundJob.locked_until < func.now()
        dead = await self.session.execute(
            update(BackgroundJob)
            .where(*expired, BackgroundJob.attempts >= BackgroundJob.max_attempts)
            .values(
                status="dead",
                locked_by=None,
                locked_until=None,
                finished_at=func.now(),
                last_error="lease expired",
            )
            .returning(
                BackgroundJob.id,
                BackgroundJob.organization_id,
                BackgroundJob.type,
                BackgroundJob.attempts,
            )
        )
        requeued: CursorResult[tuple[()]] = await self.session.execute(  # type: ignore[assignment]
            update(BackgroundJob)
            .where(*expired)
            .values(status="queued", locked_by=None, locked_until=None, last_error="lease expired")
        )
        return ReapResult(
            requeued=requeued.rowcount,
            dead=[ReapedJob(row[0], row[1], row[2], row[3]) for row in dead.all()],
        )

    async def purge_finished(self, older_than: timedelta) -> int:
        result: CursorResult[tuple[()]] = await self.session.execute(  # type: ignore[assignment]
            delete(BackgroundJob).where(
                BackgroundJob.status.in_(("succeeded", "failed")),
                BackgroundJob.finished_at < func.now() - older_than,
            )
        )
        return result.rowcount
