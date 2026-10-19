from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.services import jobs
from app.workers import handlers
from app.workers.runner import Worker, backoff
from tests.conftest import run_jobs


async def enqueue(job_type: str, payload: dict | None = None, **kwargs) -> bool:
    async with get_sessionmaker()() as session:
        inserted = await jobs.enqueue(session, job_type, payload, **kwargs)
        await session.commit()
        return inserted


async def job_row(db):
    return (
        await db.execute(
            text(
                "SELECT status, attempts, last_error, run_at > now() AS later FROM background_jobs"
            )
        )
    ).one()


@pytest.fixture
def flaky(monkeypatch):
    calls = {"count": 0}

    async def handler(session, settings, payload):
        calls["count"] += 1
        raise ConnectionError("smtp down")

    monkeypatch.setitem(handlers.HANDLERS, "flaky", handler)
    return calls


async def test_failed_job_is_requeued_with_backoff(worker, db, flaky):
    await enqueue("flaky")
    assert await worker.run_once() == 1
    row = await job_row(db)
    assert (row.status, row.attempts, row.later) == ("queued", 1, True)
    assert "ConnectionError" in row.last_error
    # Not due yet, so it is not claimed again.
    assert await worker.run_once() == 0


async def test_job_is_dead_after_max_attempts(worker, db, flaky):
    await enqueue("flaky", max_attempts=2)
    for _ in range(2):
        await worker.run_once()
        await db.execute(text("UPDATE background_jobs SET run_at = now() WHERE status = 'queued'"))
        await db.commit()
    row = await job_row(db)
    assert (row.status, row.attempts) == ("dead", 2)
    assert flaky["count"] == 2


async def test_permanent_error_is_failed_without_retry(worker, db):
    await enqueue("send_email", {"template": "does_not_exist", "to": "a@example.com"})
    await worker.run_once()
    assert (await job_row(db)).status == "failed"


async def test_unknown_job_type_is_failed(worker, db):
    await enqueue("no_such_handler")
    await worker.run_once()
    assert (await job_row(db)).status == "failed"


async def test_dedupe_key_prevents_duplicate_live_jobs(worker, db):
    assert await enqueue("purge_retention", dedupe_key="purge:1") is True
    assert await enqueue("purge_retention", dedupe_key="purge:1") is False
    await run_jobs(worker)
    # Once the first has finished, the key is free again.
    assert await enqueue("purge_retention", dedupe_key="purge:1") is True


async def test_two_workers_never_claim_the_same_job(db):
    for index in range(20):
        await enqueue("purge_retention", {"n": index})
    seen: list[str] = []

    async def record(session, settings, payload):
        seen.append(payload["n"])

    handlers.HANDLERS["purge_retention"], original = record, handlers.HANDLERS["purge_retention"]
    try:
        import asyncio

        a = Worker(get_sessionmaker(), get_settings(), worker_id="a")
        b = Worker(get_sessionmaker(), get_settings(), worker_id="b")
        await asyncio.gather(run_jobs(a), run_jobs(b))
    finally:
        handlers.HANDLERS["purge_retention"] = original
    assert sorted(seen) == list(range(20))


async def test_reaper_returns_expired_leases_to_the_queue(worker, db):
    await enqueue("purge_retention")
    await db.execute(
        text(
            "UPDATE background_jobs SET status = 'running', attempts = 1, locked_by = 'crashed', locked_until = now() - interval '1 minute'"
        )
    )
    await db.commit()
    await worker.tick()
    row = (
        await db.execute(
            text(
                "SELECT status, locked_by FROM background_jobs WHERE locked_by IS NULL AND type = 'purge_retention' AND dedupe_key IS NULL"
            )
        )
    ).one()
    assert row.status == "queued"


async def test_worker_that_lost_its_lease_cannot_overwrite_the_result(worker, db, monkeypatch):
    async def slow(session, settings, payload):
        # Another worker takes over while this handler is still running.
        async with get_sessionmaker()() as other:
            await other.execute(text("UPDATE background_jobs SET locked_by = 'other-worker'"))
            await other.commit()

    monkeypatch.setitem(handlers.HANDLERS, "slow", slow)
    await enqueue("slow")
    await worker.run_once()
    row = (await db.execute(text("SELECT status, locked_by FROM background_jobs"))).one()
    assert (row.status, row.locked_by) == ("running", "other-worker")


async def test_tick_enqueues_periodic_jobs_once(worker, db):
    await worker.tick()
    await worker.tick()
    count = await db.scalar(
        text(
            "SELECT count(*) FROM background_jobs WHERE type IN ('create_partitions', 'purge_retention')"
        )
    )
    assert count == 2
    periodic = len(handlers.PERIODIC)
    assert await run_jobs(worker) == periodic
    assert (
        await db.scalar(text("SELECT count(*) FROM background_jobs WHERE status = 'succeeded'"))
        == periodic
    )


def test_backoff_grows_and_is_capped():
    assert timedelta(seconds=24) <= backoff(1) <= timedelta(seconds=36)
    assert backoff(3) > backoff(1)
    assert backoff(50) <= timedelta(seconds=3600 * 1.2)
