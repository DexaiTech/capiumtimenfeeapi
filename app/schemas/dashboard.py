"""Response model of the dashboard."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


class DashboardWeek(BaseModel):
    week_start: date
    week_end: date
    total_minutes: int
    chargeable_minutes: int
    non_chargeable_minutes: int
    capacity_minutes: int = Field(description="The person's weekly capacity")
    utilisation_bp: int | None = Field(description="Chargeable / capacity, basis points")
    target_bp: int


class DashboardTimer(BaseModel):
    is_running: bool = Field(description="False while paused")
    started_at: datetime | None = Field(description="When the running stretch began; null paused")
    accumulated_seconds: int
    elapsed_seconds: int = Field(description="Accumulated plus the running stretch")
    client_id: uuid.UUID | None
    client_name: str | None
    job_id: uuid.UUID | None
    job_number: str | None
    job_name: str | None
    description: str | None
    is_billable: bool


class UnsubmittedWeek(BaseModel):
    timesheet_id: uuid.UUID
    week_start: date
    status: str = Field(description="`open` or `rejected`")
    total_minutes: int


class DueJob(BaseModel):
    job_id: uuid.UUID
    number: str
    name: str
    client_id: uuid.UUID
    client_name: str
    status: str
    due_date: date
    days_until_due: int = Field(description="Negative when overdue")
    is_overdue: bool
    is_manager: bool = Field(description="The caller manages the job")
    is_assignee: bool = Field(description="The caller is assigned to the job")


class ClientAmount(BaseModel):
    client_id: uuid.UUID
    client_code: str
    client_name: str
    amount: int
    oldest_date: date | None = Field(description="Oldest WIP item date, or oldest overdue due date")


class DashboardFinancial(BaseModel):
    wip_total: int = Field(description="WIP as the WIP report counts it, as at today")
    wip_over_90_days: int = Field(description="WIP items more than 90 days old")
    debtors_total: int = Field(description="Outstanding balance of issued invoices")
    overdue_total: int = Field(description="Outstanding balance past its due date")
    billed_this_month: int = Field(description="Net of invoices issued this month")
    billed_last_month: int = Field(description="Net of invoices issued last month")
    top_clients_by_wip: list[ClientAmount]
    top_overdue_debtors: list[ClientAmount]


class Dashboard(BaseModel):
    today: date = Field(description="Today in the practice time zone")
    week: DashboardWeek
    timer: DashboardTimer | None
    unsubmitted_week_count: int = Field(
        description="Past weeks whose timesheet has time and is still open or rejected"
    )
    unsubmitted_weeks: list[UnsubmittedWeek] = Field(description="The 12 most recent")
    jobs_due: list[DueJob] = Field(
        description="Open jobs the caller manages or is assigned to, due within 14 days "
        "(overdue ones included), soonest first, at most 20"
    )
    approvals_waiting: int | None = Field(
        description="Submitted timesheets waiting for the caller; null without "
        "`timesheet.approve` or `report.financial`"
    )
    financial: DashboardFinancial | None = Field(
        description="Practice figures; null without `report.financial`"
    )
