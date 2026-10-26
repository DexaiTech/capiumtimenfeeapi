"""Jobs: client work with budgets, fees, a status workflow and assignees.

Named ``time_jobs`` because ``app/services/jobs.py`` is the background-job queue.
"""

import uuid
from collections.abc import Collection, Sequence
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.db.models import Job, Task
from app.db.session import unit_of_work
from app.repositories.job_tasks import TaskRepository
from app.repositories.time_entries import TimerRepository
from app.repositories.time_jobs import ClientJobRepository, JobFilters
from app.repositories.time_refs import TimeReferenceRepository
from app.schemas.bulk import BulkChangeResult, unique_ids
from app.schemas.jobs import (
    JobAssigneesSet,
    JobBulkAction,
    JobCopy,
    JobCreate,
    JobRead,
    JobStatusChange,
    JobUpdate,
    TaskCounts,
)
from app.services import audit, policy, sequences
from app.services.audit import AuditRow
from app.services.common import record_audit_many
from app.services.job_budget_alerts import refresh_job_budgets
from app.services.pm_hooks import (
    applying_pm_change_active,
    ensure_pm_fields_free,
    queue_job_status,
)
from app.services.policy import OrgContext
from app.services.time_rules import (
    InvalidTransitionError,
    ensure_client_accepts_work,
    practice_now,
)
from app.utils.pagination import Page, PageParams

# docs/modules/jobs.md: a job starts planned (not started), is worked in progress, may be put on
# hold, is completed and then closed. Completed and closed jobs can be reopened (back to in
# progress); cancelled from an open state is final. Closed and cancelled jobs take no time.
TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"in_progress", "on_hold", "closed", "cancelled"}),
    "in_progress": frozenset({"on_hold", "completed", "closed", "cancelled"}),
    "on_hold": frozenset({"in_progress", "closed", "cancelled"}),
    "completed": frozenset({"closed", "in_progress"}),
    "closed": frozenset({"in_progress"}),
    "cancelled": frozenset(),
}

_UPDATABLE = (
    "name",
    "description",
    "service_id",
    "billing_basis",
    "fixed_fee",
    "hourly_rate",
    "budget_minutes",
    "budget_fee",
    "budget_cost",
    "period_start",
    "period_end",
    "start_date",
    "due_date",
    "partner_user_id",
    "manager_user_id",
)


# A job a bulk action changed: the audit action and what it adds to the audit meta.
JobChange = tuple[Job, str, dict[str, Any]]


class JobInUseError(ConflictError):
    code = "JOB_IN_USE"
    message = "This job has time, disbursements, expenses or invoice lines and cannot be deleted."


def _json(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def resolve_user_ref(ctx: OrgContext, value: str | None, field: str) -> uuid.UUID | None:
    """A query value that is a user id or ``me``."""
    if value is None:
        return None
    if value == "me":
        return ctx.user.id
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise ValidationError.for_field(field, "Use a user id or 'me'.") from exc


async def ensure_active_assignees(
    refs: TimeReferenceRepository, organization_id: uuid.UUID, user_ids: Collection[uuid.UUID]
) -> None:
    """422 (``user_ids`` lists who) unless every person is an active member of the practice."""
    active = await refs.active_member_ids(organization_id, user_ids)
    missing = [str(u) for u in user_ids if u not in active]
    if missing:
        raise ValidationError(
            details=[
                {
                    "field": "user_ids",
                    "code": "not_active_member",
                    "message": "Assignees must be active members of the practice.",
                    "user_ids": missing,
                }
            ]
        )


def _task_counts(found: tuple[int, int] | None) -> TaskCounts:
    total, completed = found or (0, 0)
    return TaskCounts(total=total, completed=completed)


def _within(day: date, start: date | None, end: date | None) -> bool:
    return (start is None or day >= start) and (end is None or day <= end)


class JobService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.jobs = ClientJobRepository(session)
        self.refs = TimeReferenceRepository(session)
        self.task_repo = TaskRepository(session)

    # --- Reads ---------------------------------------------------------------------------

    async def list_jobs(
        self,
        ctx: OrgContext,
        *,
        client_id: uuid.UUID | None,
        statuses: Sequence[str] | None,
        manager_user_id: str | None,
        partner_user_id: str | None,
        assignee: str | None,
        service_id: uuid.UUID | None,
        due_from: date | None,
        due_to: date | None,
        q: str | None,
        sort: str,
        params: PageParams,
    ) -> Page[JobRead]:
        filters = JobFilters(
            client_id=client_id,
            statuses=statuses,
            manager_user_id=resolve_user_ref(ctx, manager_user_id, "manager_user_id"),
            partner_user_id=resolve_user_ref(ctx, partner_user_id, "partner_user_id"),
            assignee_user_id=resolve_user_ref(ctx, assignee, "assignee"),
            service_id=service_id,
            due_from=due_from,
            due_to=due_to,
            q=q.strip() if q and q.strip() else None,
        )
        page = await self.jobs.list_page(ctx.organization_id, filters, sort, params)
        ids = [v.job.id for v in page.items]
        actuals = await self.jobs.actuals(ctx.organization_id, ids)
        assignees = await self.jobs.assignees(ctx.organization_id, ids)
        counts = await self.task_repo.counts(ctx.organization_id, ids)
        items = [
            JobRead.build(
                v,
                actuals[v.job.id],
                assignees.get(v.job.id, []),
                _task_counts(counts.get(v.job.id)),
            )
            for v in page.items
        ]
        return Page(items=items, next_cursor=page.next_cursor)

    async def get(self, ctx: OrgContext, job_id: uuid.UUID) -> JobRead:
        return await self._read(ctx, job_id)

    async def _read(self, ctx: OrgContext, job_id: uuid.UUID) -> JobRead:
        view = await self.jobs.view(ctx.organization_id, job_id)
        if view is None:
            raise NotFoundError("Job not found.")
        actuals = await self.jobs.actuals(ctx.organization_id, [job_id])
        assignees = await self.jobs.assignees(ctx.organization_id, [job_id])
        counts = await self.task_repo.counts(ctx.organization_id, [job_id])
        return JobRead.build(
            view, actuals[job_id], assignees.get(job_id, []), _task_counts(counts.get(job_id))
        )

    # --- Writes --------------------------------------------------------------------------

    async def create(
        self,
        ctx: OrgContext,
        data: JobCreate,
        rctx: RequestContext,
        meta: dict[str, Any] | None = None,
    ) -> JobRead:
        """Create a job. ``meta`` is added to the audit entry (the engagement worker marks its
        jobs with ``source``)."""
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            client = await self.refs.get_client(org_id, data.client_id)
            if client is None:
                raise NotFoundError("Client not found.", details={"field": "client_id"})
            await ensure_client_accepts_work(self.session, client)

            basis: str | None = data.billing_basis
            fixed_fee = data.fixed_fee
            if data.service_id is not None:
                service = await self.refs.get_service(org_id, data.service_id)
                if service is None:
                    raise NotFoundError("Service not found.", details={"field": "service_id"})
                if not service.is_active:
                    raise ValidationError.for_field("service_id", "This service is inactive.")
                # A service that is not billable by default makes its jobs non-billable unless
                # the request says otherwise.
                basis = basis or (
                    service.billing_basis if service.default_billable else "non_billable"
                )
                if fixed_fee is None and basis == "fixed":
                    fixed_fee = service.default_fee
            basis = basis or "hourly"
            if basis == "fixed" and fixed_fee is None:
                raise ValidationError.for_field(
                    "fixed_fee", "A fixed-fee job needs a fixed fee.", "required"
                )

            people = {
                "partner_user_id": data.partner_user_id,
                "manager_user_id": data.manager_user_id,
            }
            await self._ensure_active_members(ctx, people)
            assignees = list(dict.fromkeys(data.assignee_user_ids))
            await self._ensure_assignees(ctx, assignees)

            job = Job(
                organization_id=org_id,
                client_id=client.id,
                service_id=data.service_id,
                number=await sequences.next_number(self.session, org_id, "job"),
                name=data.name,
                description=data.description,
                status=data.status,
                billing_basis=basis,
                fixed_fee=fixed_fee,
                hourly_rate=data.hourly_rate,
                budget_minutes=data.budget_minutes,
                budget_fee=data.budget_fee,
                budget_cost=data.budget_cost,
                period_start=data.period_start,
                period_end=data.period_end,
                start_date=data.start_date,
                due_date=data.due_date,
                partner_user_id=data.partner_user_id,
                manager_user_id=data.manager_user_id,
                created_by=ctx.user.id,
            )
            await self.jobs.add(job)
            if assignees:
                await self.jobs.set_assignees(org_id, job.id, assignees)
            added = 0
            if data.add_default_tasks and data.service_id is not None:
                # Imported here: the task service imports this module.
                from app.services.service_tasks import add_templates, templates_of

                templates = await templates_of(self.session, ctx, data.service_id)
                added = len(await add_templates(self.session, ctx, rctx, job, templates, assignees))
            await self._audit(
                ctx,
                rctx,
                "job.created",
                job,
                client_name=client.name,
                extra={
                    "billing_basis": basis,
                    "fixed_fee": fixed_fee,
                    "hourly_rate": data.hourly_rate,
                    "budget_fee": data.budget_fee,
                    "budget_cost": data.budget_cost,
                    "budget_minutes": data.budget_minutes,
                    **({"default_tasks_added": added} if data.add_default_tasks else {}),
                    **(meta or {}),
                },
            )
            result = await self._read(ctx, job.id)
        return result

    async def update(
        self, ctx: OrgContext, job_id: uuid.UUID, data: JobUpdate, rctx: RequestContext
    ) -> JobRead:
        fields = data.model_fields_set
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            job = await self._lock(ctx, job_id)
            ensure_pm_fields_free(job, data)
            if "service_id" in fields and data.service_id not in (None, job.service_id):
                assert data.service_id is not None
                service = await self.refs.get_service(org_id, data.service_id)
                if service is None:
                    raise NotFoundError("Service not found.", details={"field": "service_id"})
                if not service.is_active:
                    raise ValidationError.for_field("service_id", "This service is inactive.")
            await self._ensure_active_members(
                ctx,
                {
                    f: getattr(data, f)
                    for f in ("partner_user_id", "manager_user_id")
                    if f in fields and getattr(data, f) != getattr(job, f)
                },
            )
            changes: dict[str, list[Any]] = {}
            for field in _UPDATABLE:
                if field not in fields:
                    continue
                new = getattr(data, field)
                old = getattr(job, field)
                if new != old:
                    changes[field] = [_json(old), _json(new)]
                    setattr(job, field, new)
            if job.billing_basis == "fixed" and job.fixed_fee is None:
                raise ValidationError.for_field(
                    "fixed_fee", "A fixed-fee job needs a fixed fee.", "required"
                )
            if job.period_start and job.period_end and job.period_start > job.period_end:
                raise ValidationError.for_field(
                    "period_end", "The period cannot end before it starts."
                )
            if (
                {"start_date", "due_date"} & fields
                and job.start_date
                and job.due_date
                and job.start_date > job.due_date
            ):
                raise ValidationError.for_field(
                    "due_date", "The due date cannot be before the start date."
                )
            await self._ensure_tasks_within_dates(job, changes)
            if changes:
                await self.session.flush()
                await self._audit(ctx, rctx, "job.updated", job, extra={"changes": changes})
                if "budget_minutes" in changes or "budget_fee" in changes:
                    await refresh_job_budgets(
                        self.session, org_id, [job.id], actor_user_id=ctx.user.id, rctx=rctx
                    )
            result = await self._read(ctx, job.id)
        return result

    async def change_status(
        self, ctx: OrgContext, job_id: uuid.UUID, data: JobStatusChange, rctx: RequestContext
    ) -> JobRead:
        async with unit_of_work(self.session):
            job = await self._lock(ctx, job_id)
            old = job.status
            if data.status not in TRANSITIONS[old]:
                raise InvalidTransitionError(
                    f"A {old.replace('_', ' ')} job cannot be moved to "
                    f"{data.status.replace('_', ' ')}.",
                    details={"from": old, "to": data.status, "allowed": sorted(TRANSITIONS[old])},
                )
            self._apply_status(job, data.status)
            await self.session.flush()
            await self._audit(
                ctx, rctx, "job.status_changed", job, extra={"old": old, "new": data.status}
            )
            await queue_job_status(self.session, job)
            result = await self._read(ctx, job.id)
        return result

    @staticmethod
    def _apply_status(job: Job, new: str) -> None:
        """Move the job to ``new`` (the transition is already checked) with its dates."""
        old = job.status
        job.status = new
        if new == "completed":
            job.completed_at = practice_now()
        elif new == "closed":
            job.closed_at = practice_now()
        elif old in ("completed", "closed"):  # reopened
            job.completed_at = None
        if old == "closed":
            job.closed_at = None

    async def set_assignees(
        self, ctx: OrgContext, job_id: uuid.UUID, data: JobAssigneesSet, rctx: RequestContext
    ) -> JobRead:
        org_id = ctx.organization_id
        user_ids = list(dict.fromkeys(data.user_ids))
        async with unit_of_work(self.session):
            job = await self._lock(ctx, job_id)
            await self._ensure_assignees(ctx, user_ids)
            old = (await self.jobs.assignees(org_id, [job.id])).get(job.id, [])
            await self.jobs.set_assignees(org_id, job.id, user_ids)
            if set(old) != set(user_ids):
                await self._audit(
                    ctx,
                    rctx,
                    "job.assignees_changed",
                    job,
                    extra={
                        "old": [str(u) for u in old],
                        "new": [str(u) for u in user_ids],
                    },
                )
            result = await self._read(ctx, job.id)
        return result

    async def copy(
        self, ctx: OrgContext, job_id: uuid.UUID, data: JobCopy, rctx: RequestContext
    ) -> JobRead:
        """A new planned job with the source's configuration. It starts with no time, WIP or
        invoices; its tasks (not started) and the people on the source come along as asked,
        except people who are no longer active members."""
        fields = data.model_fields_set
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            source = await self.jobs.get(org_id, job_id)
            if source is None:
                raise NotFoundError("Job not found.")
            client = await self.refs.get_client(org_id, data.client_id or source.client_id)
            if client is None:
                raise NotFoundError("Client not found.", details={"field": "client_id"})
            await ensure_client_accepts_work(self.session, client)
            service_id = data.service_id if "service_id" in fields else source.service_id
            if service_id is not None:
                service = await self.refs.get_service(org_id, service_id)
                if service is None:
                    raise NotFoundError("Service not found.", details={"field": "service_id"})
                if not service.is_active:
                    raise ValidationError.for_field("service_id", "This service is inactive.")
            start_date = data.start_date if "start_date" in fields else source.start_date
            due_date = data.due_date if "due_date" in fields else source.due_date
            if start_date and due_date and start_date > due_date:
                raise ValidationError.for_field(
                    "due_date", "The due date cannot be before the start date."
                )

            source_tasks = (
                await self.task_repo.list_for_job(org_id, source.id) if data.copy_tasks else []
            )
            job_people: list[uuid.UUID] = []
            task_people: dict[uuid.UUID, list[uuid.UUID]] = {}
            if data.copy_assignees:
                job_people = (await self.jobs.assignees(org_id, [source.id])).get(source.id, [])
                task_people = await self.task_repo.assignees(org_id, [t.id for t in source_tasks])
            wanted = {
                person
                for person in (
                    source.partner_user_id,
                    source.manager_user_id,
                    *job_people,
                    *(u for people in task_people.values() for u in people),
                )
                if person is not None
            }
            active = await self.refs.active_member_ids(org_id, wanted)

            job = Job(
                organization_id=org_id,
                client_id=client.id,
                service_id=service_id,
                number=await sequences.next_number(self.session, org_id, "job"),
                name=data.name or f"Copy of {source.name}"[:200].strip(),
                description=source.description,
                status="planned",
                billing_basis=source.billing_basis,
                fixed_fee=source.fixed_fee,
                hourly_rate=source.hourly_rate,
                budget_minutes=source.budget_minutes,
                budget_fee=source.budget_fee,
                budget_cost=source.budget_cost,
                period_start=source.period_start,
                period_end=source.period_end,
                start_date=start_date,
                due_date=due_date,
                partner_user_id=source.partner_user_id
                if source.partner_user_id in active
                else None,
                manager_user_id=source.manager_user_id
                if source.manager_user_id in active
                else None,
                created_by=ctx.user.id,
            )
            await self.jobs.add(job)
            people = [person for person in job_people if person in active]
            if people:
                await self.jobs.set_assignees(org_id, job.id, people)
            for position, task in enumerate(source_tasks):
                # A due date outside the new job's dates would break the rule for new tasks.
                keep_due = task.due_date is not None and _within(
                    task.due_date, start_date, due_date
                )
                new_task = await self.task_repo.add(
                    Task(
                        organization_id=org_id,
                        job_id=job.id,
                        title=task.title,
                        description=task.description,
                        status="not_started",
                        budget_minutes=task.budget_minutes,
                        due_date=task.due_date if keep_due else None,
                        sort_order=position,
                        created_by=ctx.user.id,
                    )
                )
                members = [person for person in task_people.get(task.id, []) if person in active]
                if members:
                    await self.task_repo.set_assignees(org_id, new_task.id, members)
            await self._audit(
                ctx,
                rctx,
                "job.copied",
                job,
                client_name=client.name,
                extra={
                    "source_job_id": str(source.id),
                    "source_number": source.number,
                    "source_name": source.name,
                    "copy_tasks": data.copy_tasks,
                    "copy_assignees": data.copy_assignees,
                    "tasks_copied": len(source_tasks),
                },
            )
            result = await self._read(ctx, job.id)
        return result

    async def delete(self, ctx: OrgContext, job_id: uuid.UUID, rctx: RequestContext) -> None:
        async with unit_of_work(self.session):
            job = await self._lock(ctx, job_id)
            if await self.jobs.in_use(ctx.organization_id, job.id):
                raise JobInUseError()
            # A running timer on the job would have nowhere to write its time.
            await TimerRepository(self.session).delete_for_job(ctx.organization_id, job.id)
            await self.jobs.soft_delete(job)
            await self._audit(ctx, rctx, "job.deleted", job)

    # --- Bulk action -----------------------------------------------------------------------

    async def bulk(
        self, ctx: OrgContext, data: JobBulkAction, rctx: RequestContext
    ) -> BulkChangeResult:
        """One action on up to 200 jobs, all or nothing: each job is checked with the rules of
        the single operation and one that breaks them (409) stops the whole action."""
        policy.ensure(ctx, "job.delete" if data.action == "delete" else "job.update")
        org_id = ctx.organization_id
        ids = unique_ids(data.job_ids)
        async with unit_of_work(self.session):
            jobs = await self.jobs.lock_many(org_id, ids)
            if len(jobs) != len(ids):
                found = {j.id for j in jobs}
                raise NotFoundError(
                    "Some of the jobs were not found.",
                    details={"job_ids": [str(i) for i in ids if i not in found]},
                )
            clients = await self.refs.get_clients(org_id, {j.client_id for j in jobs})
            changed: list[JobChange] = []
            match data.action:
                case "set_status":
                    assert data.value is not None
                    changed = self._bulk_status(jobs, data.value)
                case "set_manager":
                    changed = await self._bulk_manager(ctx, jobs, data.user_id)
                case "set_due_date":
                    changed = await self._bulk_due_date(jobs, data.day)
                case "delete":
                    changed = await self._bulk_delete(ctx, jobs)
            await self.session.flush()
            rows: list[AuditRow] = []
            for job, action, extra in changed:
                client = clients.get(job.client_id)
                meta = {
                    "number": job.number,
                    "name": job.name,
                    "client_id": str(job.client_id),
                    "client_name": client.name if client else None,
                    **extra,
                    "bulk": True,
                }
                rows.append((action, "job", job.id, meta))
            await record_audit_many(self.session, ctx, rctx, rows)
            if data.action == "set_status":
                for job, _, _ in changed:
                    await queue_job_status(self.session, job)
        return BulkChangeResult(action=data.action, affected=len(ids), changed=len(changed))

    @staticmethod
    def _refuse(code: str, message: str, details: list[dict[str, Any]]) -> ConflictError:
        return ConflictError(message, code=code, details=details)

    def _bulk_status(self, jobs: Sequence[Job], status: str) -> list[JobChange]:
        refused = [
            {
                "job_id": str(j.id),
                "number": j.number,
                "reason": f"A {j.status.replace('_', ' ')} job cannot be moved to "
                f"{status.replace('_', ' ')}.",
                "from": j.status,
                "allowed": sorted(TRANSITIONS[j.status]),
            }
            for j in jobs
            if j.status != status and status not in TRANSITIONS[j.status]
        ]
        if refused:
            raise InvalidTransitionError(
                f"{len(refused)} job{'s' if len(refused) != 1 else ''} cannot be moved to "
                f"{status.replace('_', ' ')}. Nothing was changed.",
                details=refused,
            )
        rows = []
        for job in jobs:
            old = job.status
            if old == status:
                continue
            self._apply_status(job, status)
            rows.append((job, "job.status_changed", {"old": old, "new": status}))
        return rows

    async def _bulk_manager(
        self, ctx: OrgContext, jobs: Sequence[Job], manager: uuid.UUID | None
    ) -> list[JobChange]:
        moving = [j for j in jobs if j.manager_user_id != manager]
        self._ensure_not_pm_managed(moving, "manager", "manager_user_id")
        if manager is not None and moving:
            await self._ensure_active_members(ctx, {"value": manager})
        rows = []
        for job in moving:
            old = job.manager_user_id
            job.manager_user_id = manager
            rows.append(
                (job, "job.updated", {"changes": {"manager_user_id": [_json(old), _json(manager)]}})
            )
        return rows

    async def _bulk_due_date(self, jobs: Sequence[Job], due: date | None) -> list[JobChange]:
        moving = [j for j in jobs if j.due_date != due]
        self._ensure_not_pm_managed(moving, "due date", "due_date")
        refused: list[dict[str, Any]] = []
        rows = []
        for job in moving:
            old = job.due_date
            job.due_date = due
            reason = None
            if job.start_date and due and job.start_date > due:
                reason = "The due date cannot be before the start date."
            else:
                try:
                    await self._ensure_tasks_within_dates(job, {"due_date": [None, None]})
                except ValidationError as exc:
                    reason = str(exc.details[0]["message"])
            if reason:
                refused.append({"job_id": str(job.id), "number": job.number, "reason": reason})
            rows.append((job, "job.updated", {"changes": {"due_date": [_json(old), _json(due)]}}))
        if refused:
            raise self._refuse(
                "JOB_DUE_DATE_INVALID",
                f"The due date does not fit {len(refused)} of the jobs. Nothing was changed.",
                refused,
            )
        return rows

    def _ensure_not_pm_managed(self, jobs: Sequence[Job], label: str, field: str) -> None:
        linked = [j for j in jobs if j.pm_job_id is not None and not applying_pm_change_active()]
        if linked:
            raise self._refuse(
                "JOB_FIELD_MANAGED_BY_PM",
                f"The {label} of a job linked to practice management can only be changed there.",
                [
                    {
                        "job_id": str(j.id),
                        "number": j.number,
                        "field": field,
                        "reason": f"The {label} is managed in practice management.",
                    }
                    for j in linked
                ],
            )

    async def _bulk_delete(self, ctx: OrgContext, jobs: Sequence[Job]) -> list[JobChange]:
        in_use = await self.jobs.in_use_ids(ctx.organization_id, [j.id for j in jobs])
        if in_use:
            raise self._refuse(
                JobInUseError.code,
                f"{len(in_use)} job{'s have' if len(in_use) != 1 else ' has'} time, "
                "disbursements, expenses or invoice lines and cannot be deleted. Nothing was "
                "deleted.",
                [
                    {
                        "job_id": str(j.id),
                        "number": j.number,
                        "reason": "Has time, disbursements, expenses, invoice lines or write-offs.",
                    }
                    for j in jobs
                    if j.id in in_use
                ],
            )
        timers = TimerRepository(self.session)
        for job in jobs:
            await timers.delete_for_job(ctx.organization_id, job.id)
            await self.jobs.soft_delete(job)
        return [(job, "job.deleted", {}) for job in jobs]

    # --- Helpers -------------------------------------------------------------------------

    async def _lock(self, ctx: OrgContext, job_id: uuid.UUID) -> Job:
        job = await self.jobs.get(ctx.organization_id, job_id, for_update=True)
        if job is None:
            raise NotFoundError("Job not found.")
        return job

    async def _ensure_active_members(
        self, ctx: OrgContext, people: dict[str, uuid.UUID | None]
    ) -> None:
        wanted = {f: u for f, u in people.items() if u is not None}
        active = await self.refs.active_member_ids(ctx.organization_id, set(wanted.values()))
        for field, user_id in wanted.items():
            if user_id not in active:
                raise ValidationError.for_field(
                    field, "This person must be an active member of the practice."
                )

    async def _ensure_assignees(self, ctx: OrgContext, user_ids: Collection[uuid.UUID]) -> None:
        await ensure_active_assignees(self.refs, ctx.organization_id, user_ids)

    async def _ensure_tasks_within_dates(self, job: Job, changes: dict[str, list[Any]]) -> None:
        """A date changed here may not leave an open task due outside the job's dates."""
        if not ({"start_date", "due_date"} & changes.keys()):
            return
        start = job.start_date if "start_date" in changes else None
        due = job.due_date if "due_date" in changes else None
        outside = await self.task_repo.outside_job_dates(job.organization_id, job.id, start, due)
        details = []
        for field, limit, early in (("start_date", start, True), ("due_date", due, False)):
            tasks = [
                t
                for t in outside
                if limit is not None
                and t.due_date is not None
                and (t.due_date < limit if early else t.due_date > limit)
            ]
            if tasks:
                where = "before" if early else "after"
                details.append(
                    {
                        "field": field,
                        "code": "task_outside_job_dates",
                        "message": (
                            f"{len(tasks)} open task{'s are' if len(tasks) != 1 else ' is'} due "
                            f"{where} {limit}. Change the task due dates first."
                        ),
                        "task_ids": [str(t.id) for t in tasks],
                    }
                )
        if details:
            raise ValidationError(details=details)

    async def _audit(
        self,
        ctx: OrgContext,
        rctx: RequestContext,
        action: str,
        job: Job,
        *,
        client_name: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if client_name is None:
            client = await self.refs.get_client(ctx.organization_id, job.client_id)
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
                **(extra or {}),
            },
        )
