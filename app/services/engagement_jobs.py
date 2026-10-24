"""Jobs from client engagements: the next job of an engagement is created for it.

An engagement (``client_services``) that opts in (``auto_create_jobs``) gets one job per period,
created by the daily worker (``run_due``) or on demand (``create_now``). Periods are anchored on
the engagement's ``start_date``: monthly is the start plus k months, quarterly plus 3k and
annually plus 12k (always counted from the start, so 31 January goes to 28 February and back to
31 March); a period ends the day before the next one starts, and a one-off engagement has one
period that starts on the start date and has no end.

The job of a period is *due* when ``today >= period_start - job_lead_days``, the period has not
ended, it starts on or before the engagement's end date, the engagement is active and the period
has no run row. History is never backfilled: only the latest period whose window has opened is
considered, so a worker that was down (or an engagement switched on late) creates at most one job.

One row per (engagement, period start) in ``engagement_job_runs`` makes this idempotent: a
repeated run finds the row and does nothing, and a deleted job is not recreated. A run that
failed (the client is archived, the service was deactivated, ...) is recorded as ``failed`` with
the reason and is retried by the worker at most once a day: only when the failed row's
``updated_at`` (in the practice's time zone) is before today. ``create_now`` retries at once.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from dateutil.relativedelta import relativedelta
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.db.models import Client, ClientService, EngagementJobRun, Service, User
from app.db.session import unit_of_work
from app.repositories.engagement_jobs import EngagementJobRepository
from app.repositories.memberships import MembershipRepository
from app.repositories.time_refs import TimeReferenceRepository
from app.schemas.engagement_jobs import (
    EngagementJobCreated,
    EngagementJobRef,
    EngagementJobRunRead,
    NextJobPeriod,
)
from app.schemas.jobs import JobCreate, JobRead
from app.services import audit
from app.services.billing_common import conflict, local_date, today_for
from app.services.policy import OrgContext
from app.services.time_jobs import JobService
from app.utils.ids import new_id

log = get_logger(__name__)

SCHEDULE_SOURCE = "engagement_schedule"
MANUAL_SOURCE = "engagement_manual"
RUN_LIST_LIMIT = 50
STEP_MONTHS = {"monthly": 1, "quarterly": 3, "annually": 12}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --- Periods (pure) ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Period:
    start: date
    end: date | None  # None: a one-off engagement's only period


def add_months(anchor: date, months: int) -> date:
    """``anchor`` plus ``months``, keeping its day and clamping to the month's end. Always
    counted from the anchor, never from the previous result."""
    result: date = anchor + relativedelta(months=months)
    return result


def period_at(anchor: date, frequency: str, k: int) -> Period:
    """The k-th period (0 = the first) of an engagement starting on ``anchor``."""
    if frequency == "one_off":
        if k != 0:
            raise ValueError("A one-off engagement has one period.")
        return Period(anchor, None)
    step = STEP_MONTHS[frequency]
    return Period(
        add_months(anchor, step * k), add_months(anchor, step * (k + 1)) - timedelta(days=1)
    )


def _month(day: date) -> str:
    return f"{_MONTHS[day.month - 1]} {day.year}"


def period_label(frequency: str, period: Period) -> str | None:
    """``Oct 2026`` (monthly), ``Oct 2026 - Dec 2026`` (quarterly), ``Year to Sep 2027``
    (annually: the month of the period's end); none for a one-off engagement."""
    if frequency == "one_off" or period.end is None:
        return None
    if frequency == "monthly":
        return _month(period.start)
    if frequency == "quarterly":
        return f"{_month(period.start)} - {_month(period.end)}"
    return f"Year to {_month(period.end)}"


def job_name(service_name: str, frequency: str, period: Period) -> str:
    label = period_label(frequency, period)
    return (f"{service_name} - {label}" if label else service_name)[:200]


def _opened_index(anchor: date, frequency: str, lead_days: int, today: date) -> int | None:
    """The index of the latest period whose window has opened (``today >= start - lead``)."""
    reach = today + timedelta(days=lead_days)
    if frequency == "one_off":
        return 0 if anchor <= reach else None
    if anchor > reach:
        return None
    step = STEP_MONTHS[frequency]
    months = (reach.year - anchor.year) * 12 + reach.month - anchor.month
    k = months // step + 1
    while k > 0 and period_at(anchor, frequency, k).start > reach:
        k -= 1
    return k


def _starts_in_time(period: Period | None, end_date: date | None) -> Period | None:
    if period is None or (end_date is not None and period.start > end_date):
        return None
    return period


def due_period(
    anchor: date, frequency: str, lead_days: int, end_date: date | None, today: date
) -> Period | None:
    """The period whose job is due today (before looking at its run row): the latest period whose
    window has opened, if it has not ended and starts on or before the engagement's end date."""
    k = _opened_index(anchor, frequency, lead_days, today)
    if k is None:
        return None
    period = period_at(anchor, frequency, k)
    if period.end is not None and period.end < today:
        return None
    return _starts_in_time(period, end_date)


def upcoming_period(
    anchor: date, frequency: str, lead_days: int, end_date: date | None, today: date
) -> Period | None:
    """The first period whose window has not opened yet."""
    k = _opened_index(anchor, frequency, lead_days, today)
    if frequency == "one_off":
        return _starts_in_time(Period(anchor, None), end_date) if k is None else None
    return _starts_in_time(period_at(anchor, frequency, 0 if k is None else k + 1), end_date)


def next_period(
    anchor: date,
    frequency: str,
    lead_days: int,
    end_date: date | None,
    today: date,
    done: set[date],
) -> Period | None:
    """The period that is created next: the one due now unless it has its job, else the
    upcoming one (a one-off engagement that has its job has no next period)."""
    due = due_period(anchor, frequency, lead_days, end_date, today)
    if due is not None and due.start not in done:
        return due
    return upcoming_period(anchor, frequency, lead_days, end_date, today)


def next_job_period(
    engagement: ClientService, today: date, done: set[date]
) -> NextJobPeriod | None:
    """The engagement's ``next_job_period``: null when it is off, inactive, has no start date or
    has no period left."""
    if not (engagement.auto_create_jobs and engagement.is_active) or engagement.start_date is None:
        return None
    found = next_period(
        engagement.start_date,
        engagement.frequency,
        engagement.job_lead_days,
        engagement.end_date,
        today,
        done,
    )
    if found is None:
        return None
    return NextJobPeriod(
        period_start=found.start,
        period_end=found.end,
        create_on=found.start - timedelta(days=engagement.job_lead_days),
    )


# --- Creating the job ----------------------------------------------------------------------


def _reason(exc: Exception) -> str:
    """Why a run failed, in words for the run list."""
    details = getattr(exc, "details", None)
    if isinstance(details, list) and details and isinstance(details[0], dict):
        message = details[0].get("message")
        if message:
            return str(message)[:500]
    return (getattr(exc, "message", None) or str(exc) or type(exc).__name__)[:500]


async def _system_context(session: AsyncSession, organization_id: uuid.UUID) -> OrgContext | None:
    """The worker acts as the practice's oldest active Owner (None: the practice has none)."""
    member = await EngagementJobRepository(session).oldest_owner(organization_id)
    if member is None:
        return None
    found = await MembershipRepository(session).get_active(organization_id, member.user_id)
    user = await session.get(User, member.user_id)
    if found is None or user is None:
        return None
    organization, member = found
    return OrgContext(
        user=user,
        session_id=new_id(),
        organization=organization,
        member=member,
        role=member.role,
        permissions=frozenset(p.code for p in member.role.permissions),
    )


async def _job_data(
    refs: TimeReferenceRepository,
    engagement: ClientService,
    service: Service,
    client: Client,
    period: Period,
) -> JobCreate:
    people = {u for u in (client.manager_user_id, client.partner_user_id) if u is not None}
    active = await refs.active_member_ids(engagement.organization_id, people)
    basis = service.billing_basis if service.default_billable else "non_billable"
    return JobCreate(
        client_id=client.id,
        name=job_name(service.name, engagement.frequency, period),
        service_id=service.id,
        fixed_fee=engagement.fee if basis == "fixed" else None,
        period_start=period.start,
        period_end=period.end,
        start_date=period.start,
        due_date=None,
        status="planned",
        manager_user_id=client.manager_user_id if client.manager_user_id in active else None,
        partner_user_id=client.partner_user_id if client.partner_user_id in active else None,
        add_default_tasks=True,
        assignee_user_ids=[],
    )


async def _create_job(
    session: AsyncSession,
    ctx: OrgContext,
    rctx: RequestContext,
    engagement: ClientService,
    service: Service,
    period: Period,
    run: EngagementJobRun | None,
    source: str,
) -> JobRead:
    """Create the job of one period and its run row (or settle the failed one) in the caller's
    transaction (a savepoint when failure must not break it)."""
    refs = TimeReferenceRepository(session)
    client = await refs.get_client(ctx.organization_id, engagement.client_id)
    if client is None:
        raise NotFoundError("Client not found.")
    data = await _job_data(refs, engagement, service, client, period)
    job = await JobService(session).create(
        ctx, data, rctx, meta={"source": source, "client_service_id": str(engagement.id)}
    )
    if run is None:
        run = await EngagementJobRepository(session).add_run(
            EngagementJobRun(
                id=new_id(),
                organization_id=ctx.organization_id,
                client_service_id=engagement.id,
                period_start=period.start,
                period_end=period.end,
                status="created",
                job_id=job.id,
            )
        )
    else:
        run.status, run.job_id, run.error = "created", job.id, None
        run.period_end = period.end
        run.updated_at = datetime.now(UTC)
        await session.flush()
    await audit.record(
        session,
        "engagement.job_created",
        ctx=rctx,
        actor_user_id=ctx.user.id,
        organization_id=ctx.organization_id,
        target_type="client_service",
        target_id=engagement.id,
        meta={
            "client_id": str(client.id),
            "client_name": client.name,
            "service": service.name,
            "job_id": str(job.id),
            "job_number": job.number,
            "period_start": period.start.isoformat(),
            "period_end": period.end.isoformat() if period.end else None,
            "source": source,
        },
    )
    return job


class EngagementJobService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = EngagementJobRepository(session)
        self.refs = TimeReferenceRepository(session)

    async def runs(
        self, ctx: OrgContext, client_id: uuid.UUID, engagement_id: uuid.UUID
    ) -> list[EngagementJobRunRead]:
        """The newest runs first, up to ``RUN_LIST_LIMIT``."""
        await self._engagement_of(ctx, client_id, engagement_id, lock=False)
        rows = await self.repo.runs(ctx.organization_id, engagement_id, RUN_LIST_LIMIT)
        return [
            EngagementJobRunRead(
                period_start=run.period_start,
                period_end=run.period_end,
                status=run.status,
                error=run.error,
                job=EngagementJobRef(id=job.id, number=job.number, name=job.name) if job else None,
                created_at=run.created_at,
            )
            for run, job in rows
        ]

    async def _engagement_of(
        self, ctx: OrgContext, client_id: uuid.UUID, engagement_id: uuid.UUID, *, lock: bool
    ) -> tuple[ClientService, Service, Client]:
        client = await self.refs.get_client(ctx.organization_id, client_id)
        if client is None:
            raise NotFoundError("Client not found.")
        found = await self.repo.lock_engagement(
            ctx.organization_id, engagement_id, skip_locked=False, lock=lock
        )
        if found is None or found[0].client_id != client.id:
            raise NotFoundError("Client service not found.")
        return found[0], found[1], client

    async def create_now(
        self,
        ctx: OrgContext,
        client_id: uuid.UUID,
        engagement_id: uuid.UUID,
        rctx: RequestContext,
    ) -> EngagementJobCreated:
        """Create the job of the current period (the latest whose window has opened), else of the
        upcoming one, now. 409 ``ENGAGEMENT_JOB_EXISTS`` when it has its job already and 409
        ``ENGAGEMENT_NOT_SCHEDULABLE`` (with the reason) when the engagement cannot make one.
        A failed earlier run of the period is retried."""
        async with unit_of_work(self.session):
            engagement, service, client = await self._engagement_of(
                ctx, client_id, engagement_id, lock=True
            )
            today = today_for(ctx.organization)
            if engagement.start_date is None:
                raise _not_schedulable("The service has no start date.")
            if not engagement.is_active:
                raise _not_schedulable("The service is inactive for this client.")
            if not service.is_active:
                raise _not_schedulable("The service is inactive.")
            anchor, freq, lead = (
                engagement.start_date,
                engagement.frequency,
                engagement.job_lead_days,
            )
            period = due_period(anchor, freq, lead, engagement.end_date, today) or upcoming_period(
                anchor, freq, lead, engagement.end_date, today
            )
            if period is None:
                raise _not_schedulable("The service has ended.")
            run = await self.repo.get_run(engagement.id, period.start)
            if run is not None and run.status == "created":
                raise conflict(
                    "ENGAGEMENT_JOB_EXISTS",
                    "The job of this period was already created.",
                    {"period_start": period.start.isoformat()},
                )
            try:
                job = await _create_job(
                    self.session, ctx, rctx, engagement, service, period, run, MANUAL_SOURCE
                )
            except ConflictError as exc:
                if exc.code == "CLIENT_INACTIVE":
                    raise _not_schedulable(
                        f"The client is {client.status} and does not accept new work."
                    ) from exc
                raise
        return EngagementJobCreated(job=job, period_start=period.start, period_end=period.end)


def _not_schedulable(reason: str) -> ConflictError:
    return conflict("ENGAGEMENT_NOT_SCHEDULABLE", reason, {"reason": reason})


# --- The daily worker ----------------------------------------------------------------------


async def run_due(session: AsyncSession) -> dict[str, int]:
    """Create the job of every opted-in engagement that is due, for every practice. A practice
    or engagement that fails is logged and skipped; the others still run. Idempotent: one run row
    per (engagement, period), so a repeat finds nothing left to do."""
    repo = EngagementJobRepository(session)
    horizon = datetime.now(UTC).date() + timedelta(days=1)
    totals = {"practices": 0, "created": 0, "failed": 0}
    for organization_id in await repo.organizations_with_schedules(horizon):
        try:
            created, failed = await _run_practice(session, organization_id)
        except Exception:
            log.exception("engagement_jobs_practice_failed", organization_id=str(organization_id))
            await session.rollback()
            totals["failed"] += 1
            continue
        totals["practices"] += 1
        totals["created"] += created
        totals["failed"] += failed
    return totals


async def _run_practice(session: AsyncSession, organization_id: uuid.UUID) -> tuple[int, int]:
    repo = EngagementJobRepository(session)
    ctx = await _system_context(session, organization_id)
    if ctx is None:
        log.warning("engagement_jobs_no_owner", organization_id=str(organization_id))
        return 0, 0
    today = today_for(ctx.organization)
    created = failed = 0
    for engagement_id in await repo.schedulable_ids(organization_id, today):
        try:
            outcome = await _run_one(session, ctx, engagement_id, today)
        except Exception:
            log.exception("engagement_job_failed", engagement_id=str(engagement_id))
            await session.rollback()
            # The rollback expired the context's rows: load them again.
            reloaded = await _system_context(session, organization_id)
            if reloaded is None:
                return created, failed + 1
            ctx = reloaded
            failed += 1
            continue
        created += outcome == "created"
        failed += outcome == "failed"
    return created, failed


async def _run_one(
    session: AsyncSession,
    ctx: OrgContext,
    engagement_id: uuid.UUID,
    today: date,
) -> str:
    """One engagement in its own transaction: ``created``, ``failed`` or ``skipped`` (not due,
    being run elsewhere, already has its job or failed today)."""
    repo = EngagementJobRepository(session)
    rctx = RequestContext()
    organization = ctx.organization
    async with unit_of_work(session):
        found = await repo.lock_engagement(
            organization.id, engagement_id, skip_locked=True, lock=True
        )
        if found is None:
            return "skipped"
        engagement, service = found
        if (
            not engagement.auto_create_jobs
            or not engagement.is_active
            or engagement.start_date is None
        ):
            return "skipped"
        period = due_period(
            engagement.start_date,
            engagement.frequency,
            engagement.job_lead_days,
            engagement.end_date,
            today,
        )
        if period is None:
            return "skipped"
        run = await repo.get_run(engagement.id, period.start)
        if run is not None and (
            run.status == "created" or local_date(organization, run.updated_at) >= today
        ):
            # A failed run is tried again on a later day only.
            return "skipped"
        try:
            async with session.begin_nested():
                await _create_job(
                    session, ctx, rctx, engagement, service, period, run, SCHEDULE_SOURCE
                )
        except Exception as exc:
            error = _reason(exc)
            log.warning(
                "engagement_job_failed",
                engagement_id=str(engagement_id),
                period_start=period.start.isoformat(),
                error=error,
            )
            await _record_failure(session, organization.id, engagement_id, period, error)
            return "failed"
    return "created"


async def _record_failure(
    session: AsyncSession,
    organization_id: uuid.UUID,
    engagement_id: uuid.UUID,
    period: Period,
    error: str,
) -> None:
    repo = EngagementJobRepository(session)
    run = await repo.get_run(engagement_id, period.start)
    if run is None:
        await repo.add_run(
            EngagementJobRun(
                id=new_id(),
                organization_id=organization_id,
                client_service_id=engagement_id,
                period_start=period.start,
                period_end=period.end,
                status="failed",
                error=error,
            )
        )
        return
    run.error = error
    run.updated_at = datetime.now(UTC)
    await session.flush()


__all__ = [
    "EngagementJobService",
    "Period",
    "add_months",
    "due_period",
    "job_name",
    "next_job_period",
    "next_period",
    "period_at",
    "period_label",
    "run_due",
    "upcoming_period",
]
