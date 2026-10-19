"""Budget tracking: budget against actual per job, the breakdown of one job, and the manual
re-evaluation of its alerts (the alerts themselves are in ``job_budget_alerts.py``)."""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.exceptions import NotFoundError, ValidationError
from app.db.session import unit_of_work
from app.repositories.job_budgets import (
    BudgetFilters,
    JobBudgetAlertRepository,
    JobBudgetRepository,
)
from app.repositories.job_tasks import TaskRepository
from app.repositories.time_jobs import ClientJobRepository
from app.schemas.job_budgets import (
    AlertChangeRead,
    BudgetAlertRead,
    BudgetEvaluationRead,
    BudgetFigures,
    BudgetPersonRead,
    BudgetTaskRead,
    BudgetTotalsRead,
    JobBudgetDetail,
    JobBudgetList,
    JobBudgetRow,
)
from app.services.job_budget_alerts import AlertChange, evaluate_job_budget
from app.services.policy import OrgContext, has_permission
from app.services.time_jobs import resolve_user_ref
from app.utils.job_rules import rag_of, usage_bp, worst
from app.utils.pagination import PageParams


class JobBudgetService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.budgets = JobBudgetRepository(session)
        self.alerts = JobBudgetAlertRepository(session)
        self.jobs = ClientJobRepository(session)
        self.tasks = TaskRepository(session)

    async def list_budgets(
        self,
        ctx: OrgContext,
        *,
        statuses: Sequence[str] | None,
        client_id: uuid.UUID | None,
        manager_user_id: str | None,
        partner_user_id: str | None,
        service_id: uuid.UUID | None,
        used_from_bp: int | None,
        used_to_bp: int | None,
        q: str | None,
        sort: str,
        order: str | None,
        params: PageParams,
    ) -> tuple[JobBudgetList, str | None]:
        if used_from_bp is not None and used_to_bp is not None and used_from_bp > used_to_bp:
            raise ValidationError.for_field(
                "used_to_bp", "The upper usage cannot be below the lower usage."
            )
        org_id = ctx.organization_id
        show_cost = has_permission(ctx, "rate.read")
        filters = BudgetFilters(
            statuses=statuses,
            client_id=client_id,
            manager_user_id=resolve_user_ref(ctx, manager_user_id, "manager_user_id"),
            partner_user_id=resolve_user_ref(ctx, partner_user_id, "partner_user_id"),
            service_id=service_id,
            used_from_bp=used_from_bp,
            used_to_bp=used_to_bp,
            q=q.strip() if q and q.strip() else None,
        )
        # The most used job first by default; names and numbers read A to Z.
        descending = (order or ("desc" if sort == "used" else "asc")) == "desc"
        page = await self.budgets.list_page(org_id, filters, sort, descending, params)
        totals = await self.budgets.totals(org_id, filters)
        alerts = await self.alerts.for_jobs(org_id, [row.job.id for row in page.items])
        rows = [
            JobBudgetRow.build(row, alerts.get(row.job.id, []), show_cost=show_cost)
            for row in page.items
        ]
        body = JobBudgetList(rows=rows, totals=BudgetTotalsRead.build(totals, show_cost=show_cost))
        return body, page.next_cursor

    async def detail(self, ctx: OrgContext, job_id: uuid.UUID) -> JobBudgetDetail:
        org_id = ctx.organization_id
        view = await self.jobs.view(org_id, job_id)
        if view is None:
            raise NotFoundError("Job not found.")
        job = view.job
        show_cost = has_permission(ctx, "rate.read")
        actuals = (await self.jobs.actuals(org_id, [job_id]))[job_id]
        usage = (await self.budgets.usage(org_id, [job_id]))[job_id]

        tasks = await self.tasks.list_for_job(org_id, job_id)
        task_actuals = await self.tasks.actuals(org_id, [task.id for task in tasks])
        by_task = [
            BudgetTaskRead(
                task_id=task.id,
                title=task.title,
                status=task.status,
                budget_minutes=task.budget_minutes,
                minutes=task_actuals[task.id].minutes,
                used_bp=usage_bp(task_actuals[task.id].minutes, task.budget_minutes),
                value=task_actuals[task.id].value,
                cost_value=task_actuals[task.id].cost_value if show_cost else None,
            )
            for task in tasks
        ]
        # What no task claims (time logged on the job without a task).
        unlinked_minutes = usage.logged_minutes - sum(a.minutes for a in task_actuals.values())
        unlinked_value = usage.time_value - sum(a.value for a in task_actuals.values())
        if unlinked_minutes > 0 or unlinked_value > 0:
            unlinked_cost = usage.cost_value - sum(a.cost_value for a in task_actuals.values())
            by_task.append(
                BudgetTaskRead(
                    task_id=None,
                    title=None,
                    status=None,
                    budget_minutes=None,
                    minutes=unlinked_minutes,
                    used_bp=None,
                    value=unlinked_value,
                    cost_value=unlinked_cost if show_cost else None,
                )
            )
        by_person = [
            BudgetPersonRead(
                user_id=person.user_id,
                full_name=person.full_name,
                minutes=person.minutes,
                billable_minutes=person.billable_minutes,
                value=person.value,
                cost_value=person.cost_value if show_cost else None,
            )
            for person in await self.budgets.by_person(org_id, job_id)
        ]
        alerts = (await self.alerts.for_jobs(org_id, [job_id])).get(job_id, [])
        return JobBudgetDetail(
            **BudgetFigures.values(job, usage, show_cost=show_cost),
            job_id=job.id,
            number=job.number,
            name=job.name,
            status=job.status,
            billing_basis=job.billing_basis,
            client_id=job.client_id,
            client_name=view.client_name,
            due_date=job.due_date,
            billed_value=actuals.billed_value,
            written_off_value=actuals.written_off_value,
            remaining_minutes=(
                job.budget_minutes - usage.logged_minutes if job.budget_minutes else None
            ),
            remaining_fee=job.budget_fee - usage.time_value if job.budget_fee else None,
            by_task=by_task,
            by_person=by_person,
            alerts=[BudgetAlertRead.build(alert) for alert in alerts],
        )

    async def evaluate(
        self, ctx: OrgContext, job_id: uuid.UUID, rctx: RequestContext
    ) -> BudgetEvaluationRead:
        """Raise and re-arm the job's alerts now (what every time write already does)."""
        org_id = ctx.organization_id
        async with unit_of_work(self.session):
            if await self.jobs.get(org_id, job_id) is None:
                raise NotFoundError("Job not found.")
            result = await evaluate_job_budget(
                self.session, org_id, job_id, actor_user_id=ctx.user.id, rctx=rctx
            )
            job = await self.jobs.get(org_id, job_id)
            assert job is not None
            usage = (await self.budgets.usage(org_id, [job_id]))[job_id]
            used = worst(
                usage_bp(usage.logged_minutes, job.budget_minutes),
                usage_bp(usage.time_value, job.budget_fee),
            )
            alerts = (await self.alerts.for_jobs(org_id, [job_id])).get(job_id, [])
        return BudgetEvaluationRead(
            job_id=job_id,
            used_bp=used,
            rag=rag_of(used),
            triggered=[_change(c) for c in result.triggered],
            cleared=[_change(c) for c in result.cleared],
            alerts=[BudgetAlertRead.build(alert) for alert in alerts],
        )


def _change(change: AlertChange) -> AlertChangeRead:
    return AlertChangeRead(kind=change.kind, threshold=change.threshold, usage_bp=change.usage_bp)
