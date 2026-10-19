"""The worker loop: claim jobs under a lease, run them outside the claim transaction."""

import asyncio
import contextlib
import os
import random
import socket
import time
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import BackgroundJob
from app.repositories.jobs import JobRepository
from app.services import jobs as job_service
from app.services import notification_events
from app.services.notification_events import FailedJob
from app.workers.handlers import HANDLERS, PERIODIC, PermanentJobError

log = get_logger(__name__)

TICK_SECONDS = 60
SCHEDULER_LOCK_ID = 7_301_001
BACKOFF_BASE_SECONDS, BACKOFF_MAX_SECONDS = 30, 3600


def backoff(attempts: int) -> timedelta:
    """Exponential with jitter: 30 s, 60 s, 120 s … capped at one hour."""
    seconds = min(BACKOFF_BASE_SECONDS * 2 ** max(attempts - 1, 0), BACKOFF_MAX_SECONDS)
    return timedelta(seconds=seconds * random.uniform(0.8, 1.2))  # noqa: S311


class Worker:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        worker_id: str | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._stop = asyncio.Event()
        self._last_tick = 0.0

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        log.info("worker_started", worker_id=self.worker_id)
        while not self._stop.is_set():
            try:
                if time.monotonic() - self._last_tick >= TICK_SECONDS:
                    await self.tick()
                    self._last_tick = time.monotonic()
                processed = await self.run_once()
            except Exception:
                log.exception("worker_loop_error")
                processed = 0
            if processed == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.settings.worker_poll_interval_seconds
                    )
        log.info("worker_stopped", worker_id=self.worker_id)

    async def run_once(self) -> int:
        """Claim one batch and run it. Returns the number of jobs processed."""
        async with self.sessionmaker() as session:
            claimed = await JobRepository(session).claim(
                self.worker_id, self.settings.worker_concurrency, self.settings.worker_lease_seconds
            )
            # Commit the claim at once: no row lock is held while handlers work.
            await session.commit()
        await asyncio.gather(*(self._run_job(job) for job in claimed))
        return len(claimed)

    async def _run_job(self, job: BackgroundJob) -> None:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            job_id=str(job.id), job_type=job.type, request_id=job.payload.get("request_id")
        )
        started = time.perf_counter()
        handler = HANDLERS.get(job.type)
        status, error, payload, run_at = "succeeded", None, None, None
        try:
            if handler is None:
                raise PermanentJobError(f"no handler for job type {job.type!r}")
            async with self.sessionmaker() as session:
                payload = await handler(session, self.settings, job.payload)
                await session.commit()
        except PermanentJobError as exc:
            status, error = "failed", str(exc)[:500]
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            if job.attempts >= job.max_attempts:
                status = "dead"
            else:
                status, run_at = "queued", datetime.now(UTC) + backoff(job.attempts)

        async with self.sessionmaker() as session:
            owned = await JobRepository(session).finish(
                job.id,
                self.worker_id,
                status=status,
                payload=payload,
                last_error=error,
                run_at=run_at,
            )
            if owned and status in ("failed", "dead"):
                # In the transaction that records the failure: the administrators hear of it
                # exactly when the job is final.
                await notification_events.failed_job(
                    session,
                    FailedJob(
                        job_id=job.id,
                        organization_id=job.organization_id,
                        job_type=job.type,
                        attempts=job.attempts,
                        status=status,
                    ),
                )
            await session.commit()
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        if not owned:
            log.warning("job_lease_lost", duration_ms=duration_ms)
        elif status == "succeeded":
            log.info("job_succeeded", attempts=job.attempts, duration_ms=duration_ms)
        elif status == "queued":
            log.warning("job_retry", attempts=job.attempts, error=error, duration_ms=duration_ms)
        else:
            log.error("job_" + status, attempts=job.attempts, error=error, duration_ms=duration_ms)

    async def tick(self) -> None:
        """Reap expired leases and enqueue periodic jobs. One worker at a time."""
        async with self.sessionmaker() as session:
            locked = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": SCHEDULER_LOCK_ID}
            )
            if not locked:
                return
            reaped = await JobRepository(session).reap_expired()
            if reaped.total:
                log.warning("jobs_reaped", count=reaped.total)
            for dead in reaped.dead:
                await notification_events.failed_job(
                    session,
                    FailedJob(
                        job_id=dead.id,
                        organization_id=dead.organization_id,
                        job_type=dead.type,
                        attempts=dead.attempts,
                        status="dead",
                    ),
                )
            now = int(time.time())
            jobs = JobRepository(session)
            keys = {job_type: f"{job_type}:{now // period}" for job_type, period in PERIODIC}
            # The dedupe key only guards live jobs; a slot whose job already finished must
            # not be scheduled again by the next tick.
            done = await jobs.existing_dedupe_keys(list(keys.values()))
            for job_type, key in keys.items():
                if key not in done:
                    await job_service.enqueue(session, job_type, dedupe_key=key)
            await session.commit()
