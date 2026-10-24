"""Request and response models of the job endpoints."""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.repositories.time_jobs import JobActuals, JobView
from app.schemas.bulk import (
    MAX_BULK_IDS,
    need_value,
    parse_day,
    parse_user_id,
)
from app.schemas.common import RequestModel
from app.utils.money import percentage_bp

JobStatus = Literal["planned", "in_progress", "on_hold", "completed", "closed", "cancelled"]
JobBillingBasis = Literal["hourly", "fixed", "non_billable"]
JobSort = Literal["due_date", "-due_date", "number", "-number", "created_at", "-created_at"]
Money = Annotated[int, Field(ge=0, le=10**12, description="Minor units (pence)")]
Minutes = Annotated[int, Field(ge=0, le=10_000_000, description="Whole minutes")]
UserIds = Annotated[list[uuid.UUID], Field(max_length=100)]


class TaskCounts(BaseModel):
    total: int = Field(description="Tasks of the job")
    completed: int = Field(description="Tasks of the job with status completed")


class JobRead(BaseModel):
    id: uuid.UUID
    number: str
    name: str
    description: str | None
    client_id: uuid.UUID
    client_code: str
    client_name: str
    service_id: uuid.UUID | None
    service_name: str | None
    status: JobStatus
    billing_basis: JobBillingBasis
    fixed_fee: int | None
    hourly_rate: int | None
    budget_minutes: int | None
    budget_fee: int | None
    period_start: date | None
    period_end: date | None
    start_date: date | None
    due_date: date | None
    partner_user_id: uuid.UUID | None
    manager_user_id: uuid.UUID | None
    assignee_user_ids: list[uuid.UUID]
    completed_at: datetime | None
    closed_at: datetime | None = Field(description="When the job was closed; null unless closed")
    pm_job_id: str | None = Field(
        default=None,
        description=(
            "The practice management job this job is linked to (null: a local job). A linked "
            "job's name, service, manager, partner and dates are managed there (409 "
            "`JOB_FIELD_MANAGED_BY_PM` on edit)"
        ),
    )
    pm_linked_at: datetime | None = Field(
        default=None, description="When the job was linked to practice management"
    )
    task_counts: TaskCounts
    created_at: datetime
    updated_at: datetime
    # Actuals
    logged_minutes: int = Field(description="All time logged on the job")
    billable_minutes: int = Field(description="Billable time logged on the job")
    time_value: int = Field(description="Charge value of all billable time logged")
    wip_value: int = Field(description="Charge value of billable time not yet billed")
    billed_value: int = Field(
        description="Billed value of the job's time and disbursements on issued invoices"
    )
    written_off_value: int = Field(description="WIP value written off without an invoice")
    budget_minutes_used_bp: int | None = Field(
        description="logged_minutes / budget_minutes in basis points; null without a budget"
    )
    budget_fee_used_bp: int | None = Field(
        description="time_value / budget_fee in basis points; null without a budget"
    )

    @classmethod
    def build(
        cls,
        view: JobView,
        actuals: JobActuals,
        assignees: list[uuid.UUID],
        task_counts: TaskCounts | None = None,
    ) -> "JobRead":
        job = view.job
        return cls(
            id=job.id,
            number=job.number,
            name=job.name,
            description=job.description,
            client_id=job.client_id,
            client_code=view.client_code,
            client_name=view.client_name,
            service_id=job.service_id,
            service_name=view.service_name,
            status=job.status,
            billing_basis=job.billing_basis,
            fixed_fee=job.fixed_fee,
            hourly_rate=job.hourly_rate,
            budget_minutes=job.budget_minutes,
            budget_fee=job.budget_fee,
            period_start=job.period_start,
            period_end=job.period_end,
            start_date=job.start_date,
            due_date=job.due_date,
            partner_user_id=job.partner_user_id,
            manager_user_id=job.manager_user_id,
            assignee_user_ids=assignees,
            completed_at=job.completed_at,
            closed_at=job.closed_at,
            pm_job_id=job.pm_job_id,
            pm_linked_at=job.pm_linked_at,
            task_counts=task_counts or TaskCounts(total=0, completed=0),
            created_at=job.created_at,
            updated_at=job.updated_at,
            logged_minutes=actuals.logged_minutes,
            billable_minutes=actuals.billable_minutes,
            time_value=actuals.time_value,
            wip_value=actuals.wip_value,
            billed_value=actuals.billed_value,
            written_off_value=actuals.written_off_value,
            budget_minutes_used_bp=(
                percentage_bp(actuals.logged_minutes, job.budget_minutes)
                if job.budget_minutes
                else None
            ),
            budget_fee_used_bp=(
                percentage_bp(actuals.time_value, job.budget_fee) if job.budget_fee else None
            ),
        )


def _check_period(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and start > end:
        raise ValueError("period_end: The period cannot end before it starts")


def _check_dates(start: date | None, due: date | None) -> None:
    if start is not None and due is not None and start > due:
        raise ValueError("due_date: The due date cannot be before the start date")


class JobCreate(RequestModel):
    client_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    service_id: uuid.UUID | None = None
    billing_basis: JobBillingBasis | None = Field(
        default=None, description="Default: the service's basis, else hourly"
    )
    fixed_fee: Money | None = Field(
        default=None, description="Default for fixed jobs: the service's default fee"
    )
    hourly_rate: Money | None = Field(
        default=None, description="Overrides every other charge-out rate for time on this job"
    )
    budget_minutes: Minutes | None = None
    budget_fee: Money | None = None
    period_start: date | None = None
    period_end: date | None = None
    start_date: date | None = None
    due_date: date | None = None
    partner_user_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    status: Literal["planned", "in_progress"] = "planned"
    assignee_user_ids: UserIds = Field(default_factory=list)
    add_default_tasks: bool = Field(
        default=False,
        description=(
            "Also add the service's default tasks (`GET /services/{id}/task-templates`) as the "
            "job's tasks: each is due `due_offset_days` after the job's start date when that "
            "lies within the job's dates, and is assigned to the job's assignees. Ignored "
            "without a service or when the service has none."
        ),
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        _check_period(self.period_start, self.period_end)
        _check_dates(self.start_date, self.due_date)
        return self


class JobUpdate(RequestModel):
    """Every field optional. The client and the status are not changed here (status has
    its own endpoint)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    service_id: uuid.UUID | None = None
    billing_basis: JobBillingBasis | None = None
    fixed_fee: Money | None = None
    hourly_rate: Money | None = None
    budget_minutes: Minutes | None = None
    budget_fee: Money | None = None
    period_start: date | None = None
    period_end: date | None = None
    start_date: date | None = None
    due_date: date | None = None
    partner_user_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for field in ("name", "billing_basis"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field}: This field cannot be null")
        _check_dates(self.start_date, self.due_date)
        return self


class JobStatusChange(RequestModel):
    status: JobStatus


JobBulkActionName = Literal["set_status", "set_manager", "set_due_date", "delete"]
_JOB_STATUSES = ("planned", "in_progress", "on_hold", "completed", "closed", "cancelled")


class JobBulkAction(RequestModel):
    job_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_BULK_IDS)
    action: JobBulkActionName
    value: str | None = Field(
        default=None,
        description=(
            "`set_status`: a job status; `set_manager`: a staff user id, or null to clear the "
            "manager; `set_due_date`: a date (YYYY-MM-DD), or null to clear it; `delete`: "
            "no value"
        ),
    )

    @model_validator(mode="after")
    def _value_fits_the_action(self) -> Self:
        value = self.value
        if self.action == "delete":
            if value is not None:
                raise ValueError("value: This action takes no value.")
        elif self.action == "set_status":
            if need_value(value) not in _JOB_STATUSES:
                raise ValueError(f"value: Use one of {', '.join(_JOB_STATUSES)}.")
        elif value is not None and self.action == "set_manager":
            parse_user_id(value)
        elif value is not None and self.action == "set_due_date":
            parse_day(value)
        return self

    @property
    def user_id(self) -> uuid.UUID | None:
        return parse_user_id(self.value) if self.value is not None else None

    @property
    def day(self) -> date | None:
        return parse_day(self.value) if self.value is not None else None


class JobAssigneesSet(RequestModel):
    user_ids: UserIds = Field(description="The complete list of assignees (active members)")


class JobCopy(RequestModel):
    """What a copy changes; everything else is taken from the source job. A field that is
    left out is copied; ``service_id``, ``start_date`` and ``due_date`` may be sent as null
    to leave them empty on the copy."""

    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Default: 'Copy of ' and the source job's name (cut to 200 characters)",
    )
    client_id: uuid.UUID | None = Field(
        default=None, description="Default: the source job's client"
    )
    service_id: uuid.UUID | None = Field(
        default=None, description="Default: the source job's service; null for none"
    )
    start_date: date | None = Field(
        default=None, description="Default: the source job's start date; null for none"
    )
    due_date: date | None = Field(
        default=None, description="Default: the source job's due date; null for none"
    )
    copy_tasks: bool = Field(
        default=True, description="Copy the tasks (as not started), without time"
    )
    copy_assignees: bool = Field(
        default=True,
        description="Copy the job's assignees and, with the tasks, the tasks' assignees",
    )

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for field in ("name", "client_id"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field}: This field cannot be null")
        _check_dates(self.start_date, self.due_date)
        return self
