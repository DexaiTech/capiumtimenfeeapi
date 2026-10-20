"""Practice management (Phase 2A): link jobs and tasks to PM's, keep their statuses in step.

* **Import** (``import_jobs``, "run sync now"): every PM job is matched by ``pm_job_id`` and is
  created, linked or updated, **idempotently**: a second import changes nothing. A PM job is
  imported for a client the practice has enabled from the central client service (PM names the
  client by its central id); other jobs are skipped with the reason. A job of the same client and
  name that is not linked yet is linked instead of duplicated. Tasks are matched by ``pm_task_id``
  (or linked by title) and created, updated and moved to PM's status.
* **PM owns** the name, service, manager, partner and dates of a linked job: they are overwritten by
  PM's values here and refused when changed anywhere else (``pm_hooks``). Deadlines are not synced.
* **Statuses sync both ways.** A status change here queues an outbound item (``pm_hooks``) that the
  runner pushes to PM. A change of status at PM (an event, or the import) is applied through the
  **same transition rules** as a change made here (``JobService.change_status`` and its transition
  table, ``TaskService.change_status``): a change those rules refuse is kept as a failed sync item
  for a person, not forced through.
"""

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.db.models import Client, IntegrationSyncItem, Job, Task
from app.db.session import unit_of_work
from app.repositories.pm_links import PmLinkRepository
from app.schemas.integration_events import EventOutcome, JobOutcome, PmImportResult, PmJobResult
from app.schemas.job_tasks import TaskStatus, TaskStatusChange
from app.schemas.jobs import JobStatus, JobStatusChange
from app.services import audit, sequences
from app.services.common import apply_changes
from app.services.integration_events import Applied, Handled, Polled, handle_event_once, poll_feed
from app.services.integration_logs import record_failure, record_sync
from app.services.integration_outbox import ItemFailure, ItemHandler, ItemOutcome
from app.services.integrations import (
    PRACTICE_MANAGEMENT,
    IntegrationUnavailableError,
    PmEvent,
    PmJob,
    PmTask,
    PracticeManagementGateway,
    get_practice_management_gateway,
)
from app.services.job_tasks import LOCKED_JOB_STATUSES, TaskService
from app.services.pm_hooks import applying_pm_change
from app.services.policy import OrgContext
from app.services.time_jobs import JobService
from app.services.time_rules import (
    CLIENT_WORK_STATUSES,
    InvalidTransitionError,
    JobClosedError,
    ensure_client_accepts_work,
)

log = get_logger(__name__)

PM_TO_JOB_STATUS: dict[str, JobStatus] = {
    "not_started": "planned",
    "in_progress": "in_progress",
    "on_hold": "on_hold",
    "completed": "completed",
    "closed": "closed",
    "cancelled": "cancelled",
}
JOB_TO_PM_STATUS = {job: pm for pm, job in PM_TO_JOB_STATUS.items()}
MAX_IMPORT_PAGES = 10


def _task_status(value: str | None) -> TaskStatus | None:
    """``value`` when practice management's task status is one Time & Fees has."""
    match value:
        case "not_started" | "in_progress" | "on_hold" | "completed":
            return value
        case _:
            return None


def _now() -> datetime:
    return datetime.now(UTC)


def _elevated(ctx: OrgContext) -> OrgContext:
    """PM acts with the right to change job and task statuses, whoever pressed the button."""
    return replace(ctx, permissions=ctx.permissions | {"job.update"})


def _result(
    pm_job: PmJob,
    outcome: JobOutcome,
    message: str,
    *,
    job: Job | None = None,
    warnings: list[str] | None = None,
) -> PmJobResult:
    return PmJobResult(
        pm_job_id=pm_job.pm_job_id,
        job_id=job.id if job else None,
        job_number=job.number if job else None,
        name=pm_job.name,
        outcome=outcome,
        message=message,
        warnings=warnings or [],
    )


def _event_dict(event: PmEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "type": event.type,
        "pm_job_id": event.pm_job_id,
        "pm_task_id": event.pm_task_id,
        "status": event.status,
        "occurred_at": event.occurred_at.isoformat(),
    }


def event_from_payload(raw: dict[str, Any]) -> PmEvent:
    occurred = raw.get("occurred_at")
    return PmEvent(
        event_id=str(raw["event_id"]),
        type=raw["type"],
        pm_job_id=str(raw["pm_job_id"]),
        occurred_at=datetime.fromisoformat(occurred) if occurred else _now(),
        pm_task_id=raw.get("pm_task_id"),
        status=raw.get("status"),
    )


@dataclass(frozen=True, slots=True)
class _Refs:
    service_id: uuid.UUID | None
    billing_basis: str
    fixed_fee: int | None
    manager_user_id: uuid.UUID | None
    partner_user_id: uuid.UUID | None


class PmSyncService:
    def __init__(self, session: AsyncSession, gateway: PracticeManagementGateway) -> None:
        self.session = session
        self.gateway = gateway
        self.links = PmLinkRepository(session)
        self.jobs = JobService(session)
        self.tasks = TaskService(session)

    # --- Import ---------------------------------------------------------------------------

    async def import_jobs(
        self, ctx: OrgContext, rctx: RequestContext, *, cursor: str | None, limit: int
    ) -> PmImportResult:
        org_id = ctx.organization_id
        try:
            page = await self.gateway.list_jobs(cursor, limit)
        except IntegrationUnavailableError:
            async with unit_of_work(self.session):
                await record_sync(
                    self.session,
                    org_id,
                    integration=PRACTICE_MANAGEMENT,
                    operation="import",
                    status="failed",
                    message="Practice management is unavailable.",
                )
            return PmImportResult(
                available=False,
                processed=0,
                created=0,
                linked=0,
                updated=0,
                unchanged=0,
                skipped=0,
                failed=0,
                has_more=False,
                next_cursor=cursor,
                results=[],
            )
        results = [await self._import_one(ctx, rctx, pm_job) for pm_job in page.jobs]
        counts = {
            name: sum(1 for r in results if r.outcome == name)
            for name in ("created", "linked", "updated", "unchanged", "skipped", "failed")
        }
        async with unit_of_work(self.session):
            await record_sync(
                self.session,
                org_id,
                integration=PRACTICE_MANAGEMENT,
                operation="import",
                status="failed" if counts["failed"] else "ok",
                message=(
                    f"Read {len(results)} job(s): {counts['created']} created, "
                    f"{counts['linked']} linked, {counts['updated']} updated, "
                    f"{counts['unchanged']} unchanged, {counts['skipped']} skipped"
                    + (f", {counts['failed']} failed." if counts["failed"] else ".")
                ),
                details=counts,
            )
        return PmImportResult(
            available=True,
            processed=len(results),
            has_more=page.has_more,
            next_cursor=page.cursor,
            results=results,
            **counts,
        )

    async def _import_one(
        self, ctx: OrgContext, rctx: RequestContext, pm_job: PmJob
    ) -> PmJobResult:
        try:
            async with unit_of_work(self.session):
                with applying_pm_change():
                    result = await self._upsert_job(ctx, rctx, pm_job, create=True)
                if result.outcome != "unchanged":
                    await record_sync(
                        self.session,
                        ctx.organization_id,
                        integration=PRACTICE_MANAGEMENT,
                        operation="import_job",
                        status="skipped" if result.outcome == "skipped" else "ok",
                        message=f"{pm_job.pm_job_id}: {result.message}",
                        entity_type="job" if result.job_id else None,
                        entity_id=result.job_id,
                        external_id=pm_job.pm_job_id,
                        details={"outcome": result.outcome, "warnings": result.warnings},
                    )
                return result
        except Exception as exc:
            log.exception("pm_import_failed", pm_job_id=pm_job.pm_job_id)
            message = f"Could not import this job ({type(exc).__name__})."
            await record_failure(
                ctx.organization_id,
                integration=PRACTICE_MANAGEMENT,
                operation="import_job",
                external_id=pm_job.pm_job_id,
                message=f"{pm_job.pm_job_id}: {message}",
            )
            return _result(pm_job, "failed", message)

    async def _upsert_job(
        self, ctx: OrgContext, rctx: RequestContext, pm_job: PmJob, *, create: bool
    ) -> PmJobResult:
        """Create, link or update the job for a PM job. Call inside the unit of work and
        ``applying_pm_change``; returns the outcome, never raises for a missing client."""
        org_id = ctx.organization_id
        warnings: list[str] = []
        job = await self.links.job_by_pm_id(org_id, pm_job.pm_job_id, lock=True)
        linked_now = False
        client: Client | None = None
        if job is None:
            if not create:
                return _result(
                    pm_job, "skipped", "This practice management job is not linked here."
                )
            client = await self.links.client_by_central_id(org_id, pm_job.central_client_id)
            if client is None:
                return _result(
                    pm_job,
                    "skipped",
                    f"Client {pm_job.central_client_id} is not enabled in this practice: enable it "
                    "from the central client service first.",
                )
            if (
                client.merged_into_client_id is not None
                or client.status not in CLIENT_WORK_STATUSES
            ):
                return _result(pm_job, "skipped", f"Client {client.code} does not accept new work.")
            await ensure_client_accepts_work(self.session, client)
            job = await self.links.unlinked_job_named(org_id, client.id, pm_job.name)
            linked_now = job is not None
        refs = await self._resolve(org_id, pm_job, warnings)
        start, due = pm_job.start_date, pm_job.due_date
        if start is not None and due is not None and start > due:
            warnings.append("The start date is after the due date: the dates were not applied.")
            start, due = (job.start_date, job.due_date) if job else (None, None)
        target_status = PM_TO_JOB_STATUS.get(pm_job.status)
        if target_status is None:
            warnings.append(f"Unknown status {pm_job.status}: the status was not applied.")

        if job is None:
            assert client is not None
            job = await self._create_job(ctx, rctx, pm_job, client, refs, start, due, target_status)
            await self._sync_tasks(ctx, rctx, job, pm_job.tasks, warnings)
            return _result(
                pm_job, "created", f"Created job {job.number}.", job=job, warnings=warnings
            )

        changes = apply_changes(
            job,
            {
                "name": pm_job.name[:200],
                "service_id": refs.service_id,
                "manager_user_id": refs.manager_user_id,
                "partner_user_id": refs.partner_user_id,
                "start_date": start,
                "due_date": due,
            },
        )
        if linked_now:
            job.pm_job_id = pm_job.pm_job_id
            job.pm_linked_at = _now()
        if changes or linked_now:
            await self.session.flush()
            await self._audit_job(
                ctx,
                rctx,
                "job.pm_linked" if linked_now else "job.updated",
                job,
                {
                    "changes": changes,
                    "pm_job_id": pm_job.pm_job_id,
                    "source": "practice_management",
                },
            )
        status_changed = False
        if target_status is not None and target_status != job.status:
            status_changed = await self._change_job_status(ctx, rctx, job, target_status, warnings)
        tasks_changed = await self._sync_tasks(ctx, rctx, job, pm_job.tasks, warnings)
        if linked_now:
            outcome, message = "linked", f"Linked to existing job {job.number}."
        elif changes or status_changed or tasks_changed:
            outcome, message = "updated", f"Updated job {job.number}."
        else:
            outcome, message = "unchanged", "Already up to date."
        return _result(pm_job, outcome, message, job=job, warnings=warnings)

    async def _resolve(self, org_id: uuid.UUID, pm_job: PmJob, warnings: list[str]) -> _Refs:
        service_id: uuid.UUID | None = None
        basis, fee = "hourly", None
        if pm_job.service_code:
            service = await self.links.service_by_code(org_id, pm_job.service_code)
            if service is None:
                warnings.append(f"Service {pm_job.service_code} does not exist here: none was set.")
            elif not service.is_active:
                warnings.append(f"Service {service.code} is inactive: none was set.")
            else:
                service_id = service.id
                basis = service.billing_basis if service.default_billable else "non_billable"
                fee = service.default_fee if basis == "fixed" else None
                if basis == "fixed" and fee is None:
                    basis = "hourly"
        people: dict[str, uuid.UUID | None] = {}
        for role, email in (("manager", pm_job.manager_email), ("partner", pm_job.partner_email)):
            people[role] = None
            if email:
                found = await self.links.active_member_id_by_email(org_id, email)
                if found is None:
                    warnings.append(f"The {role} {email} is not an active member of this practice.")
                people[role] = found
        return _Refs(service_id, basis, fee, people["manager"], people["partner"])

    async def _create_job(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        pm_job: PmJob,
        client: Client,
        refs: _Refs,
        start: date | None,
        due: date | None,
        status: JobStatus | None,
    ) -> Job:
        org_id = ctx.organization_id
        now = _now()
        status = status or "planned"
        job = Job(
            organization_id=org_id,
            client_id=client.id,
            service_id=refs.service_id,
            number=await sequences.next_number(self.session, org_id, "job"),
            name=pm_job.name[:200],
            status=status,
            billing_basis=refs.billing_basis,
            fixed_fee=refs.fixed_fee,
            start_date=start,
            due_date=due,
            manager_user_id=refs.manager_user_id,
            partner_user_id=refs.partner_user_id,
            completed_at=now if status == "completed" else None,
            closed_at=now if status == "closed" else None,
            pm_job_id=pm_job.pm_job_id,
            pm_linked_at=now,
            created_by=ctx.user.id,
        )
        self.session.add(job)
        await self.session.flush()
        await self._audit_job(
            ctx,
            rctx,
            "job.created",
            job,
            {
                "billing_basis": refs.billing_basis,
                "fixed_fee": refs.fixed_fee,
                "pm_job_id": pm_job.pm_job_id,
                "source": "practice_management",
            },
            client_name=client.name,
        )
        return job

    async def _change_job_status(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        job: Job,
        target: JobStatus,
        warnings: list[str],
    ) -> bool:
        try:
            await self.jobs.change_status(
                _elevated(ctx), job.id, JobStatusChange(status=target), rctx
            )
        except InvalidTransitionError as exc:
            warnings.append(f"The status was not changed: {exc.message}")
            return False
        return True

    async def _sync_tasks(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        job: Job,
        pm_tasks: tuple[PmTask, ...],
        warnings: list[str],
    ) -> bool:
        if not pm_tasks:
            return False
        if job.status in LOCKED_JOB_STATUSES:
            warnings.append("Tasks were not changed: the job is closed or cancelled.")
            return False
        org_id = ctx.organization_id
        existing = await self.links.tasks_of_job(org_id, job.id)
        by_pm = {task.pm_task_id: task for task in existing if task.pm_task_id}
        unlinked = [task for task in existing if task.pm_task_id is None]
        changed = False
        for pm_task in pm_tasks:
            task = by_pm.get(pm_task.pm_task_id)
            title = pm_task.title[:200]
            if task is None:
                task = next((t for t in unlinked if t.title.lower() == title.lower()), None)
                if task is not None:
                    unlinked.remove(task)
                    task.pm_task_id = pm_task.pm_task_id
                    task.pm_linked_at = _now()
                    changed = True
            if task is None:
                await self._create_task(ctx, rctx, job, pm_task, warnings)
                changed = True
                continue
            changes = apply_changes(task, {"title": title, "due_date": pm_task.due_date})
            if changes:
                await self.session.flush()
                await self._audit_task(
                    ctx, rctx, "task.updated", job, task, {"changes": changes, **_source(pm_task)}
                )
                changed = True
            target = _task_status(pm_task.status)
            if target is None:
                warnings.append(f"Task {pm_task.title}: unknown status {pm_task.status}.")
            elif target != task.status:
                try:
                    await self.tasks.change_status(
                        _elevated(ctx), job.id, task.id, TaskStatusChange(status=target), rctx
                    )
                except JobClosedError:
                    warnings.append(f"Task {task.title}: the job is closed, the status was kept.")
                else:
                    changed = True
        return changed

    async def _create_task(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        job: Job,
        pm_task: PmTask,
        warnings: list[str],
    ) -> None:
        status = _task_status(pm_task.status) or "not_started"
        if status != pm_task.status:
            warnings.append(f"Task {pm_task.title}: unknown status {pm_task.status}.")
        now = _now()
        task = Task(
            organization_id=ctx.organization_id,
            job_id=job.id,
            title=pm_task.title[:200],
            status=status,
            due_date=pm_task.due_date,
            sort_order=await self.links.next_sort_order(ctx.organization_id, job.id),
            completed_at=now if status == "completed" else None,
            pm_task_id=pm_task.pm_task_id,
            pm_linked_at=now,
            created_by=ctx.user.id,
        )
        self.session.add(task)
        await self.session.flush()
        await self._audit_task(
            ctx, rctx, "task.created", job, task, {"status": status, **_source(pm_task)}
        )

    # --- Events ---------------------------------------------------------------------------

    async def handle_event(
        self, ctx: OrgContext, rctx: RequestContext, event: PmEvent, *, source: str
    ) -> Handled:
        return await handle_event_once(
            self.session,
            ctx.organization_id,
            integration=PRACTICE_MANAGEMENT,
            event_id=event.event_id,
            event_type=event.type,
            external_id=event.pm_job_id,
            source=source,
            apply=lambda: self.apply_event(ctx, rctx, event),
            park_payload=_event_dict(event),
            label=f"{event.type} {event.pm_job_id}",
        )

    async def apply_event(self, ctx: OrgContext, rctx: RequestContext, event: PmEvent) -> Applied:
        """Apply one PM event to the linked job or task (see ``handle_event_once`` for the
        rules about repeats, skips and refusals)."""
        org_id = ctx.organization_id
        with applying_pm_change():
            if event.type == "job_updated":
                pm_job = await self.gateway.get_job(event.pm_job_id)
                if pm_job is None:
                    return Applied("skipped", "Practice management no longer has this job.")
                result = await self._upsert_job(ctx, rctx, pm_job, create=False)
                outcome: EventOutcome = (
                    "applied"
                    if result.outcome == "updated"
                    else "failed"
                    if result.outcome == "failed"
                    else "skipped"
                )
                return Applied(
                    outcome,
                    result.message,
                    "job" if result.job_id else None,
                    result.job_id,
                    {"warnings": result.warnings},
                )
            job = await self.links.job_by_pm_id(org_id, event.pm_job_id, lock=True)
            if job is None:
                return Applied("skipped", "This practice management job is not linked here.")
            if event.type == "job_status_changed":
                return await self._apply_job_status(ctx, rctx, job, event.status)
            return await self._apply_task_status(ctx, rctx, job, event)

    async def _apply_job_status(
        self, ctx: OrgContext, rctx: RequestContext, job: Job, pm_status: str | None
    ) -> Applied:
        target = PM_TO_JOB_STATUS.get(pm_status or "")
        if target is None:
            return Applied(
                "failed",
                f"Practice management sent an unknown job status ({pm_status}).",
                "job",
                job.id,
                failure_code="UNKNOWN_STATUS",
            )
        if target == job.status:
            return Applied("skipped", "The job already has this status.", "job", job.id)
        try:
            await self.jobs.change_status(
                _elevated(ctx), job.id, JobStatusChange(status=target), rctx
            )
        except InvalidTransitionError as exc:
            return Applied(
                "failed",
                f"Job {job.number} was not changed: {exc.message}",
                "job",
                job.id,
                failure_code="INVALID_TRANSITION",
            )
        return Applied(
            "applied", f"Job {job.number} is now {target.replace('_', ' ')}.", "job", job.id
        )

    async def _apply_task_status(
        self, ctx: OrgContext, rctx: RequestContext, job: Job, event: PmEvent
    ) -> Applied:
        assert event.pm_task_id is not None
        task = await self.links.task_by_pm_id(ctx.organization_id, event.pm_task_id)
        if task is None or task.job_id != job.id:
            return Applied("skipped", "This practice management task is not linked here.")
        target = _task_status(event.status)
        if target is None:
            return Applied(
                "failed",
                f"Practice management sent an unknown task status ({event.status}).",
                "task",
                task.id,
                failure_code="UNKNOWN_STATUS",
            )
        if target == task.status:
            return Applied("skipped", "The task already has this status.", "task", task.id)
        try:
            await self.tasks.change_status(
                _elevated(ctx), job.id, task.id, TaskStatusChange(status=target), rctx
            )
        except JobClosedError as exc:
            return Applied(
                "failed",
                f"Task {task.title} was not changed: {exc.message}",
                "task",
                task.id,
                failure_code="JOB_CLOSED",
            )
        return Applied(
            "applied", f"Task {task.title} is now {target.replace('_', ' ')}.", "task", task.id
        )

    async def process_changes(self, ctx: OrgContext, rctx: RequestContext) -> Polled:
        return await poll_feed(
            self.session,
            ctx.organization_id,
            integration=PRACTICE_MANAGEMENT,
            fetch=self.gateway.changes_since,
            handle=lambda event: self.handle_event(ctx, rctx, event, source="feed"),
            service_name="practice management system",
        )

    # --- Unlinking ------------------------------------------------------------------------

    async def unlink(self, ctx: OrgContext, job_id: uuid.UUID, rctx: RequestContext) -> Job:
        """Make a linked job a local job again: its PM-owned fields become editable, its tasks
        are unlinked and what was queued for PM is dropped. 409 ``JOB_NOT_LINKED`` otherwise."""
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            job = await self.links.job(org_id, job_id, lock=True)
            if job is None:
                raise NotFoundError("Job not found.")
            if job.pm_job_id is None:
                raise ConflictError(
                    "This job is not linked to practice management.", code="JOB_NOT_LINKED"
                )
            previous = job.pm_job_id
            job.pm_job_id = None
            job.pm_linked_at = None
            task_ids = [task.id for task in await self.links.tasks_of_job(org_id, job.id)]
            await self.session.execute(
                update(Task)
                .where(Task.organization_id == org_id, Task.job_id == job.id)
                .values(pm_task_id=None, pm_linked_at=None)
            )
            await self.session.execute(
                update(IntegrationSyncItem)
                .where(
                    IntegrationSyncItem.organization_id == org_id,
                    IntegrationSyncItem.integration == PRACTICE_MANAGEMENT,
                    IntegrationSyncItem.entity_id.in_([job.id, *task_ids]),
                    IntegrationSyncItem.status.in_(("pending", "failed")),
                )
                .values(
                    status="skipped",
                    next_attempt_at=None,
                    completed_at=_now(),
                    last_error="The job was unlinked from practice management.",
                    last_error_code="UNLINKED",
                )
            )
            await self.session.flush()
            await self._audit_job(ctx, rctx, "job.pm_unlinked", job, {"pm_job_id": previous})
            await record_sync(
                self.session,
                org_id,
                integration=PRACTICE_MANAGEMENT,
                operation="unlink",
                status="ok",
                message=f"{job.number} was unlinked from {previous}.",
                entity_type="job",
                entity_id=job.id,
                external_id=previous,
            )
        return job

    # --- Audit ----------------------------------------------------------------------------

    async def _audit_job(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        action: str,
        job: Job,
        extra: dict[str, Any],
        *,
        client_name: str | None = None,
    ) -> None:
        if client_name is None:
            client = await self.links.client_of(job)
            client_name = client.name if client else None
        await audit.record(
            self.session,
            action,
            ctx=rctx,
            actor_user_id=ctx.user.id,
            organization_id=ctx.organization_id,
            target_type="job",
            target_id=job.id,
            meta={
                "number": job.number,
                "name": job.name,
                "client_id": str(job.client_id),
                "client_name": client_name,
                **extra,
            },
        )

    async def _audit_task(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        action: str,
        job: Job,
        task: Task,
        extra: dict[str, Any],
    ) -> None:
        await audit.record(
            self.session,
            action,
            ctx=rctx,
            actor_user_id=ctx.user.id,
            organization_id=ctx.organization_id,
            target_type="task",
            target_id=task.id,
            meta={
                "title": task.title,
                "job_id": str(job.id),
                "job_number": job.number,
                "job_name": job.name,
                "client_id": str(job.client_id),
                **extra,
            },
        )


def _source(pm_task: PmTask) -> dict[str, Any]:
    return {"pm_task_id": pm_task.pm_task_id, "source": "practice_management"}


# --- Handlers of sync items ---------------------------------------------------------------


async def push_status_item(session: AsyncSession, item: IntegrationSyncItem) -> ItemOutcome:
    """The handler of ``push_job_status`` and ``push_task_status`` items: set the status at PM.
    The gateway's idempotency key names the change (``change_id``), so a retry of one change is
    applied once and a later change to the same status is applied again."""
    gateway = get_practice_management_gateway()
    payload = item.payload
    pm_job_id, status = payload.get("pm_job_id"), payload.get("status")
    if not isinstance(pm_job_id, str) or not isinstance(status, str):
        raise ItemFailure("The item carries no PM job or status.", "INVALID")
    key = f"{item.idempotency_key}:{payload.get('change_id', item.attempts)}"
    if item.operation == "push_job_status":
        pm_status = JOB_TO_PM_STATUS.get(status)
        if pm_status is None:
            raise ItemFailure(
                f"Practice management has no equivalent of {status}.", "UNKNOWN_STATUS"
            )
        receipt = await gateway.set_job_status(pm_job_id, pm_status, idempotency_key=key)
    else:
        pm_task_id = payload.get("pm_task_id")
        if not isinstance(pm_task_id, str):
            raise ItemFailure("The item carries no PM task.", "INVALID")
        receipt = await gateway.set_task_status(pm_job_id, pm_task_id, status, idempotency_key=key)
    return ItemOutcome("ok", external_ref=receipt.external_ref)


def apply_event_handler(ctx: OrgContext, rctx: RequestContext) -> ItemHandler:
    """The handler that retries a refused PM event kept as a failed item (a person retries it,
    so the caller's context is at hand)."""

    async def handler(session: AsyncSession, item: IntegrationSyncItem) -> ItemOutcome:
        raw = item.payload.get("event")
        if not isinstance(raw, dict):
            raise ItemFailure("The item carries no event.", "INVALID")
        service = PmSyncService(session, get_practice_management_gateway())
        applied = await service.apply_event(ctx, rctx, event_from_payload(raw))
        if applied.outcome == "failed":
            raise ItemFailure(applied.message, applied.failure_code or "REFUSED")
        return ItemOutcome(
            "ok" if applied.outcome == "applied" else "skipped",
            message=None if applied.outcome == "applied" else applied.message,
        )

    return handler
