"""The Staff Cost report (ticket APT81, phase D; docs/modules/reports.md, "Staff Cost"): the
standard and overtime hours and cost of each person per overtime period, and the jobs worked in
overtime. The only place overtime cost is shown."""

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.overtime import OvertimeClientRef, OvertimeJobRef, OvertimeStatus
from app.schemas.reports import PagedQuery, PagedReport, ReportFilters

OvertimeType = Literal["allocated", "unallocated"]


class StaffCostQuery(PagedQuery):
    date_from: date | None = Field(
        None, description="Time worked on or after this day. Default: the first of date_to's month"
    )
    date_to: date | None = Field(
        None,
        description="Time worked on or before this day. Default: today in the practice time zone",
    )
    user_id: uuid.UUID | None = Field(None, description="Only this person")
    grade_id: uuid.UUID | None = Field(
        None, description="Only people of this grade (their current)"
    )
    job_id: uuid.UUID | None = Field(None, description="Only time on this job")
    client_id: uuid.UUID | None = Field(None, description="Only time for this client")
    status: list[OvertimeStatus] = Field(
        default_factory=list,
        description="Only overtime in these statuses (repeat the parameter); rows without any "
        "are left out. Default: every status",
    )
    overtime_type: OvertimeType | None = Field(
        None,
        description="Only `allocated` (client jobs) or `unallocated` (Internal client) overtime; "
        "rows without any are left out",
    )


class StaffCostFilters(ReportFilters):
    grade_id: uuid.UUID | None = None
    status: list[OvertimeStatus] = Field(default_factory=list)
    overtime_type: OvertimeType | None = None


class StaffCostFigures(BaseModel):
    standard_minutes: int = Field(
        description="Minutes within the standard hours (rejected overtime included)"
    )
    standard_cost: int = Field(description="Their cost at each person's standard cost rate")
    overtime_minutes_allocated: int = Field(
        description="Overtime minutes on client jobs (pending and approved)"
    )
    overtime_cost_allocated: int = Field(description="Their cost at the overtime rates applied")
    overtime_minutes_unallocated: int = Field(
        description="Overtime minutes on the Internal client's jobs (pending and approved)"
    )
    overtime_cost_unallocated: int = Field(description="Their cost at the overtime rates applied")
    total_cost: int = Field(
        description="standard_cost + overtime_cost_allocated + overtime_cost_unallocated"
    )
    pending_overtime_cost: int = Field(
        description="The part of the overtime cost still pending approval (unapproved)"
    )


class StaffCostRow(StaffCostFigures):
    user_id: uuid.UUID = Field(description="The staff member")
    name: str = Field(description="Their name")
    staff_code: str | None = Field(description="Their staff code")
    grade_name: str | None = Field(description="Their current grade")
    basis: Literal["weekly", "monthly"] | None = Field(
        description="The working hours basis of the period; null for days without working hours "
        "(counted by week, all standard)"
    )
    period_start: date = Field(
        description="First day of the period: Monday of the week or the 1st of the month, or "
        "where the working hours or the report's range start"
    )
    period_end: date = Field(description="Last day of the period, cut to the report's range")
    standard_hours_minutes: int | None = Field(
        description="The standard minutes of the period's working hours; null without hours"
    )


class StaffCostReport(PagedReport[StaffCostRow, StaffCostFigures]):
    filters: StaffCostFilters


class StaffCostJobsQuery(StaffCostQuery):
    pass


class StaffCostJobRow(BaseModel):
    job: OvertimeJobRef | None = Field(description="The job worked in overtime")
    client: OvertimeClientRef | None = Field(description="The job's client")
    allocated: bool = Field(description="false: a job of the Internal client")
    overtime: bool = Field(description="The Overtime flag: always true (every row is overtime)")
    overtime_minutes: int = Field(description="Overtime minutes on the job with this status")
    overtime_rate: int = Field(
        description="The overtime rate applied, pence per hour; when the minutes were costed at "
        "more than one rate, their average (overtime_cost per hour, rounded half up)"
    )
    overtime_cost: int = Field(description="The overtime cost of those minutes")
    status: OvertimeStatus = Field(description="pending, approved or rejected")
    entry_count: int = Field(description="The time entries with overtime on the job")

    # Flat names for the CSV, Excel and PDF columns (not part of the JSON).
    @property
    def job_number(self) -> str | None:
        return self.job.number if self.job else None

    @property
    def job_name(self) -> str | None:
        return self.job.name if self.job else None

    @property
    def client_name(self) -> str | None:
        return self.client.name if self.client else None


class StaffCostJobTotals(BaseModel):
    overtime_minutes: int = Field(description="Overtime minutes of the rows, rejected excluded")
    overtime_cost: int = Field(description="Overtime cost of the rows, rejected excluded")


class StaffCostJobsReport(PagedReport[StaffCostJobRow, StaffCostJobTotals]):
    filters: StaffCostFilters
