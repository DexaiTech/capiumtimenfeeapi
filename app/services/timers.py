"""The running timer: one per person; stopping it writes a time entry (docs/domain.md)."""

import math

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError
from app.db.models import Timer
from app.db.session import unit_of_work
from app.repositories.time_entries import TimerRepository
from app.repositories.time_jobs import ClientJobRepository
from app.repositories.time_refs import TimeReferenceRepository
from app.schemas.time import TimeEntryCreate, TimeEntryRead, TimerRead, TimerStart, TimerStop
from app.services.policy import OrgContext
from app.services.time_entries import TimeEntryService
from app.services.time_rules import (
    MAX_DAY_MINUTES,
    ensure_client_accepts_work,
    ensure_job_open,
    practice_date,
    practice_now,
)
from app.services.time_tasks import load_tasks, task_ref
from app.utils.ids import new_id


class TimerRunningError(ConflictError):
    code = "TIMER_RUNNING"
    message = "You already have a timer. Stop it first, or start with replace: true."


class TimerNotRunningError(ConflictError):
    code = "TIMER_NOT_RUNNING"
    message = "The timer is paused."


def elapsed_seconds(timer: Timer) -> int:
    seconds = timer.accumulated_seconds
    if timer.started_at is not None:
        seconds += max(0, int((practice_now() - timer.started_at).total_seconds()))
    return seconds


def elapsed_minutes(seconds: int) -> int:
    """Whole minutes, rounded up, at least one and at most a day (the practice increment
    is applied when the entry is written)."""
    return min(MAX_DAY_MINUTES, max(1, math.ceil(seconds / 60)))


class TimerService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.timers = TimerRepository(session)
        self.jobs = ClientJobRepository(session)
        self.refs = TimeReferenceRepository(session)
        self.time = TimeEntryService(session)

    def _read(self, ctx: OrgContext, timer: Timer) -> TimerRead:
        return TimerRead.build(
            timer, elapsed_seconds(timer), practice_date(ctx.organization, timer.created_at)
        )

    async def get(self, ctx: OrgContext) -> TimerRead | None:
        timer = await self.timers.get(ctx.organization_id, ctx.user.id)
        return self._read(ctx, timer) if timer else None

    async def start(self, ctx: OrgContext, data: TimerStart, rctx: RequestContext) -> TimerRead:
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            # Lock order everywhere: job(s) FOR KEY SHARE, then the timer row (deleting a job
            # locks the job, then deletes its timers). With replace, the old timer's job is
            # locked here too, because stopping it writes time on that job.
            peek = await self.timers.get(org_id, ctx.user.id) if data.replace else None
            job_ids = {j for j in (data.job_id, peek.job_id if peek else None) if j}
            jobs = await self.jobs.get_many(org_id, job_ids, key_share=True)
            job = self.time.job_ref(jobs, data.job_id, "")
            # Tasks are locked before the timer row (see time_tasks.py), the old timer's too.
            tasks = await load_tasks(
                self.session, org_id, [data.task_id, peek.task_id if peek else None]
            )
            task = task_ref(tasks, data.task_id, job, "task_id")
            client_ids = {c for c in (data.client_id, job.client_id if job else None) if c}
            client = self.time.client_ref(
                await self.refs.get_clients(org_id, client_ids), data.client_id, job, ""
            )
            activity = self.time.activity_ref(
                await self.refs.get_activity_codes(
                    org_id, {data.activity_code_id} if data.activity_code_id else set()
                ),
                data.activity_code_id,
                "",
                check=True,
            )
            if client is not None:
                await ensure_client_accepts_work(self.session, client)
            if job is not None:
                ensure_job_open(job)
                allowed = await self.refs.allowed_activities(
                    org_id, {job.service_id} if job.service_id else set()
                )
                self.time.ensure_activity_allowed(allowed, job, activity, "")
            billable = self.time.resolve_billable(data.is_billable, job, activity, client, "")

            existing = await self.timers.get(org_id, ctx.user.id, for_update=True)
            if existing is not None:
                if not data.replace:
                    raise TimerRunningError(details={"timer_id": str(existing.id)})
                await self._stop(ctx, existing, None, rctx)

            timer = Timer(
                id=new_id(),
                organization_id=org_id,
                user_id=ctx.user.id,
                client_id=client.id if client else None,
                job_id=job.id if job else None,
                task_id=task.id if task else None,
                activity_code_id=activity.id if activity else None,
                description=data.description,
                is_billable=billable,
                started_at=practice_now(),
                accumulated_seconds=0,
            )
            if not await self.timers.insert_if_absent(timer):
                raise TimerRunningError()
            created = await self.timers.get(org_id, ctx.user.id)
            assert created is not None
            result = self._read(ctx, created)
        return result

    async def pause(self, ctx: OrgContext) -> TimerRead:
        async with unit_of_work(self.session):
            timer = await self._lock(ctx)
            if timer.started_at is None:
                raise TimerNotRunningError()
            timer.accumulated_seconds = elapsed_seconds(timer)
            timer.started_at = None
            await self.session.flush()
            result = self._read(ctx, timer)
        return result

    async def resume(self, ctx: OrgContext) -> TimerRead:
        async with unit_of_work(self.session):
            timer = await self._lock(ctx)
            if timer.started_at is not None:
                raise TimerRunningError("The timer is already running.")
            timer.started_at = practice_now()
            await self.session.flush()
            result = self._read(ctx, timer)
        return result

    async def stop(
        self, ctx: OrgContext, data: TimerStop | None, rctx: RequestContext
    ) -> TimeEntryRead:
        async with unit_of_work(self.session):
            # Job first, then the timer (the order job deletion uses), so a stop racing a
            # job delete waits instead of deadlocking.
            peek = await self.timers.get(ctx.organization_id, ctx.user.id)
            if peek is not None and peek.job_id is not None:
                await self.jobs.get_many(ctx.organization_id, {peek.job_id}, key_share=True)
            if peek is not None:
                await load_tasks(self.session, ctx.organization_id, [peek.task_id])
            timer = await self._lock(ctx)
            entry = await self._stop(ctx, timer, data, rctx)
        return entry

    async def discard(self, ctx: OrgContext) -> None:
        async with unit_of_work(self.session):
            timer = await self._lock(ctx)
            await self.timers.delete(timer)

    async def _stop(
        self, ctx: OrgContext, timer: Timer, data: TimerStop | None, rctx: RequestContext
    ) -> TimeEntryRead:
        description = timer.description
        if data is not None and "description" in data.model_fields_set:
            description = data.description
        entry = await self.time.create(
            ctx,
            TimeEntryCreate(
                entry_date=practice_date(ctx.organization, timer.created_at),
                minutes=elapsed_minutes(elapsed_seconds(timer)),
                client_id=timer.client_id,
                job_id=timer.job_id,
                task_id=timer.task_id,
                activity_code_id=timer.activity_code_id,
                description=description,
                is_billable=timer.is_billable,
            ),
            rctx,
            source="timer",
        )
        await self.timers.delete(timer)
        return entry

    async def _lock(self, ctx: OrgContext) -> Timer:
        timer = await self.timers.get(ctx.organization_id, ctx.user.id, for_update=True)
        if timer is None:
            raise NotFoundError("You have no timer.", code="TIMER_NOT_FOUND")
        return timer
