"""Budget alerts: tell the people on a job when it has used 80% and 100% of its budget.

``evaluate_job_budget`` compares the job's usage with its budgets (hours and fee) and keeps
``job_budget_alerts`` in step: a threshold the job is at or above gets a row (once, however many
times the job is evaluated) and its people are notified; a threshold the job has fallen back
below loses its row, so crossing it again raises the alert again. It runs in the caller's
transaction and locks the job ``FOR NO KEY UPDATE``, so evaluations of one job queue up and each
sees the time the others committed.

``refresh_job_budgets`` is what the write paths call: the same inside a savepoint, so that a
problem with an alert can never fail the time or disbursement being saved.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.logging import get_logger
from app.db.models import Job
from app.repositories.job_budgets import JobBudgetAlertRepository, JobBudgetRepository
from app.repositories.time_jobs import ClientJobRepository
from app.repositories.time_refs import TimeReferenceRepository
from app.services import audit, notification_events
from app.utils.job_rules import ALERT_KINDS, ALERT_THRESHOLDS, OPEN_JOB_STATUSES, usage_bp

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AlertChange:
    kind: str
    threshold: int
    usage_bp: int | None


@dataclass(slots=True)
class BudgetEvaluation:
    job_id: uuid.UUID
    triggered: list[AlertChange] = field(default_factory=list)
    cleared: list[AlertChange] = field(default_factory=list)


async def evaluate_job_budget(
    session: AsyncSession,
    organization_id: uuid.UUID,
    job_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
    rctx: RequestContext | None = None,
) -> BudgetEvaluation:
    """Raise and re-arm the job's budget alerts; does not commit.

    A threshold is raised only while the job is open (planned, in progress or on hold): the
    time of a finished job cannot grow. An unknown or deleted job changes nothing.
    ``actor_user_id`` and ``rctx`` attribute the audit rows to the person whose write caused it.
    """
    result = BudgetEvaluation(job_id=job_id)
    job = await ClientJobRepository(session).lock_no_key(organization_id, job_id)
    if job is None:
        return result
    usage = (await JobBudgetRepository(session).usage(organization_id, [job.id]))[job.id]
    used = {
        "minutes": usage_bp(usage.logged_minutes, job.budget_minutes),
        "fee": usage_bp(usage.time_value, job.budget_fee),
    }
    alerts = JobBudgetAlertRepository(session)
    held = {
        (alert.kind, alert.threshold)
        for alert in (await alerts.for_jobs(organization_id, [job.id])).get(job.id, [])
    }
    is_open = job.status in OPEN_JOB_STATUSES
    raised: dict[tuple[str, int], uuid.UUID] = {}
    for kind in ALERT_KINDS:
        bp = used[kind]
        for threshold in ALERT_THRESHOLDS:
            key = (kind, threshold)
            if bp is None or bp < threshold * 100:
                if key in held:
                    await alerts.remove(organization_id, job.id, kind, threshold)
                    result.cleared.append(AlertChange(kind, threshold, bp))
            elif key not in held and is_open:
                alert_id = await alerts.add(organization_id, job.id, kind, threshold, bp)
                if alert_id is not None:
                    raised[key] = alert_id
                    result.triggered.append(AlertChange(kind, threshold, bp))
    if raised:
        await _announce(
            session, organization_id, job, result.triggered, raised, actor_user_id, rctx
        )
    return result


async def _announce(
    session: AsyncSession,
    organization_id: uuid.UUID,
    job: Job,
    triggered: list[AlertChange],
    raised: dict[tuple[str, int], uuid.UUID],
    actor_user_id: uuid.UUID | None,
    rctx: RequestContext | None,
) -> None:
    """Audit every threshold crossed; notify once per budget, for the highest it reached."""
    client = await TimeReferenceRepository(session).get_client(organization_id, job.client_id)
    client_name = client.name if client else ""
    for change in triggered:
        await audit.record(
            session,
            "job.budget_alert",
            ctx=rctx,
            actor_user_id=actor_user_id,
            organization_id=organization_id,
            target_type="job",
            target_id=job.id,
            meta={
                "number": job.number,
                "name": job.name,
                "client_id": str(job.client_id),
                "client_name": client_name,
                "kind": change.kind,
                "threshold": change.threshold,
                "usage_bp": change.usage_bp,
            },
        )
    assignees = await JobBudgetAlertRepository(session).assignee_ids(organization_id, job.id)
    recipients = list(
        dict.fromkeys(
            person
            for person in (job.manager_user_id, job.partner_user_id, *assignees)
            if person is not None
        )
    )
    for kind in ALERT_KINDS:
        crossed = [change.threshold for change in triggered if change.kind == kind]
        if not crossed:
            continue
        threshold = max(crossed)
        await notification_events.budget_alert(
            session,
            organization_id=organization_id,
            recipients=recipients,
            job_id=job.id,
            job_name=job.name,
            client_name=client_name,
            percent=threshold,
            kind=kind,
            crossing=str(raised[(kind, threshold)]),
        )


async def refresh_job_budgets(
    session: AsyncSession,
    organization_id: uuid.UUID,
    job_ids: Iterable[uuid.UUID | None],
    *,
    actor_user_id: uuid.UUID | None = None,
    rctx: RequestContext | None = None,
) -> None:
    """:func:`evaluate_job_budget` for each job (in id order, so concurrent writers lock in the
    same order), inside a savepoint. An error is logged, never raised."""
    for job_id in sorted({job_id for job_id in job_ids if job_id is not None}):
        try:
            async with session.begin_nested():
                await evaluate_job_budget(
                    session, organization_id, job_id, actor_user_id=actor_user_id, rctx=rctx
                )
        except Exception:
            log.exception("job_budget_evaluation_failed", job_id=str(job_id))
