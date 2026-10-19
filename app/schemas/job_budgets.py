"""Request and response models of budget tracking: budget against actual per job, by task and by
person, and the alerts a job has raised. Money is pence, durations whole minutes, usage basis
points (10000 = 100%)."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.db.models import Job, JobBudgetAlert
from app.repositories.job_budgets import BudgetRow, BudgetTotals, JobUsage
from app.schemas.jobs import JobBillingBasis, JobStatus
from app.utils.job_rules import Rag, rag_of, usage_bp, worst
from app.utils.money import percentage_bp

BudgetSort = Literal["used", "number", "name", "client", "due_date", "logged_minutes", "time_value"]
SortOrder = Literal["asc", "desc"]
AlertKind = Literal["minutes", "fee"]


class BudgetAlertRead(BaseModel):
    kind: AlertKind = Field(description="minutes: the hours budget; fee: the fee budget")
    threshold: Literal[80, 100] = Field(description="Per cent of the budget")
    triggered_at: datetime
    usage_bp: int = Field(description="Usage when the threshold was crossed")

    @classmethod
    def build(cls, alert: JobBudgetAlert) -> "BudgetAlertRead":
        return cls(
            kind=alert.kind,
            threshold=alert.threshold,
            triggered_at=alert.triggered_at,
            usage_bp=alert.usage_bp,
        )


class BudgetFigures(BaseModel):
    """Budget against actual. Hours: ``logged_minutes`` of every entry. Fee: ``time_value`` is the
    charge value of the billable time, as on the job."""

    budget_minutes: int | None
    logged_minutes: int
    billable_minutes: int
    budget_minutes_used_bp: int | None = Field(description="Null without an hours budget")
    budget_fee: int | None
    time_value: int
    wip_value: int = Field(description="Billable time not yet billed")
    budget_fee_used_bp: int | None = Field(description="Null without a fee budget")
    used_bp: int | None = Field(description="The worse of the hours and the fee usage")
    rag: Rag | None = Field(
        description="green: under 80%; amber: 80% up to 100%; red: 100% or more"
    )
    cost_value: int | None = Field(description="Cost of the time; null without rate.read")
    margin_value: int | None = Field(
        description="time_value minus cost_value; null without rate.read"
    )
    margin_bp: int | None = Field(
        description="margin_value / time_value; null without rate.read or without value"
    )

    @staticmethod
    def values(job: Job, usage: JobUsage, *, show_cost: bool) -> dict[str, object]:
        minutes_bp = usage_bp(usage.logged_minutes, job.budget_minutes)
        fee_bp = usage_bp(usage.time_value, job.budget_fee)
        used = worst(minutes_bp, fee_bp)
        margin = usage.time_value - usage.cost_value if show_cost else None
        return {
            "budget_minutes": job.budget_minutes,
            "logged_minutes": usage.logged_minutes,
            "billable_minutes": usage.billable_minutes,
            "budget_minutes_used_bp": minutes_bp,
            "budget_fee": job.budget_fee,
            "time_value": usage.time_value,
            "wip_value": usage.wip_value,
            "budget_fee_used_bp": fee_bp,
            "used_bp": used,
            "rag": rag_of(used),
            "cost_value": usage.cost_value if show_cost else None,
            "margin_value": margin,
            "margin_bp": percentage_bp(margin, usage.time_value) if margin is not None else None,
        }


class JobBudgetRow(BudgetFigures):
    job_id: uuid.UUID
    number: str
    name: str
    status: JobStatus
    billing_basis: JobBillingBasis
    client_id: uuid.UUID
    client_code: str
    client_name: str
    service_id: uuid.UUID | None
    service_name: str | None
    manager_user_id: uuid.UUID | None
    manager_name: str | None
    partner_user_id: uuid.UUID | None
    partner_name: str | None
    start_date: date | None
    due_date: date | None
    alerts: list[BudgetAlertRead] = Field(description="Thresholds the job has crossed and is above")

    @classmethod
    def build(
        cls, row: BudgetRow, alerts: list[JobBudgetAlert], *, show_cost: bool
    ) -> "JobBudgetRow":
        job = row.job
        return cls(
            **BudgetFigures.values(job, row.usage, show_cost=show_cost),
            job_id=job.id,
            number=job.number,
            name=job.name,
            status=job.status,
            billing_basis=job.billing_basis,
            client_id=job.client_id,
            client_code=row.client_code,
            client_name=row.client_name,
            service_id=job.service_id,
            service_name=row.service_name,
            manager_user_id=job.manager_user_id,
            manager_name=row.manager_name,
            partner_user_id=job.partner_user_id,
            partner_name=row.partner_name,
            start_date=job.start_date,
            due_date=job.due_date,
            alerts=[BudgetAlertRead.build(a) for a in alerts],
        )


class BudgetTotalsRead(BaseModel):
    """Over every job the filters select, not only the page."""

    jobs: int
    green: int
    amber: int
    red: int
    budget_minutes: int = Field(description="Sum over the jobs that have an hours budget")
    logged_minutes: int = Field(description="Time logged on every job in the list")
    budget_minutes_used_bp: int | None = Field(
        description="Time logged on the jobs with an hours budget / their budgets"
    )
    budget_fee: int = Field(description="Sum over the jobs that have a fee budget")
    time_value: int = Field(description="Charge value of the billable time of every job")
    wip_value: int
    budget_fee_used_bp: int | None = Field(
        description="Time value of the jobs with a fee budget / their budgets"
    )
    cost_value: int | None = Field(description="Null without rate.read")
    margin_value: int | None = Field(description="Null without rate.read")
    margin_bp: int | None = Field(description="Null without rate.read")

    @classmethod
    def build(cls, totals: BudgetTotals, *, show_cost: bool) -> "BudgetTotalsRead":
        margin = totals.time_value - totals.cost_value if show_cost else None
        return cls(
            jobs=totals.jobs,
            green=totals.green,
            amber=totals.amber,
            red=totals.red,
            budget_minutes=totals.budget_minutes,
            logged_minutes=totals.logged_minutes,
            budget_minutes_used_bp=usage_bp(totals.minutes_basis_logged, totals.budget_minutes),
            budget_fee=totals.budget_fee,
            time_value=totals.time_value,
            wip_value=totals.wip_value,
            budget_fee_used_bp=usage_bp(totals.fee_basis_value, totals.budget_fee),
            cost_value=totals.cost_value if show_cost else None,
            margin_value=margin,
            margin_bp=percentage_bp(margin, totals.time_value) if margin is not None else None,
        )


class JobBudgetList(BaseModel):
    rows: list[JobBudgetRow]
    totals: BudgetTotalsRead


class BudgetPersonRead(BaseModel):
    user_id: uuid.UUID
    full_name: str
    minutes: int
    billable_minutes: int
    value: int = Field(description="Charge value of the person's billable time")
    cost_value: int | None = Field(description="Null without rate.read")


class BudgetTaskRead(BaseModel):
    task_id: uuid.UUID | None = Field(description="Null for time that is not linked to a task")
    title: str | None
    status: str | None
    budget_minutes: int | None
    minutes: int
    used_bp: int | None = Field(description="minutes / budget_minutes; null without a budget")
    value: int = Field(description="Charge value of the billable time")
    cost_value: int | None = Field(description="Null without rate.read")


class JobBudgetDetail(BudgetFigures):
    job_id: uuid.UUID
    number: str
    name: str
    status: JobStatus
    billing_basis: JobBillingBasis
    client_id: uuid.UUID
    client_name: str
    due_date: date | None
    billed_value: int = Field(description="Billed value on issued invoices")
    written_off_value: int
    remaining_minutes: int | None = Field(
        description="budget_minutes minus logged_minutes (negative when over); null without budget"
    )
    remaining_fee: int | None = Field(
        description="budget_fee minus time_value (negative when over); null without a budget"
    )
    by_task: list[BudgetTaskRead]
    by_person: list[BudgetPersonRead]
    alerts: list[BudgetAlertRead] = Field(description="Thresholds the job has crossed and is above")


class AlertChangeRead(BaseModel):
    kind: AlertKind
    threshold: Literal[80, 100]
    usage_bp: int | None


class BudgetEvaluationRead(BaseModel):
    job_id: uuid.UUID
    used_bp: int | None
    rag: Rag | None
    triggered: list[AlertChangeRead] = Field(description="Thresholds crossed by this evaluation")
    cleared: list[AlertChangeRead] = Field(
        description="Thresholds the job is back below (they can be raised again)"
    )
    alerts: list[BudgetAlertRead] = Field(description="Thresholds in force after the evaluation")
