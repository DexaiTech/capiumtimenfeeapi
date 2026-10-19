"""Request and response models of the time, timer, timesheet and approval endpoints."""

import uuid
from datetime import date, datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.db.models import Timer, Timesheet
from app.repositories.time_entries import EntryView
from app.repositories.timesheets import SheetTotals
from app.schemas.common import RequestModel

TimesheetStatus = Literal["open", "submitted", "approved", "rejected"]
BillingStatus = Literal["unbilled", "in_draft", "billed", "written_off"]
EDITABLE_SHEET_STATUSES = ("open", "rejected")

EntryMinutes = Annotated[int, Field(ge=1, le=1440, description="Whole minutes, 1-1440")]
# Bounded so week arithmetic (week start, week end) can never overflow the date range.
MIN_TIME_DATE, MAX_TIME_DATE = date(1900, 1, 1), date(2999, 12, 31)
EntryDate = Annotated[date, Field(ge=MIN_TIME_DATE, le=MAX_TIME_DATE)]
Description = Annotated[str, Field(max_length=2000)]
InternalNote = Annotated[str, Field(max_length=2000)]
NOTE_DESCRIPTION = (
    "Internal note (up to 2000 characters), never printed on an invoice; blank clears it"
)


def blank_note_to_none(value: str | None) -> str | None:
    return value or None


# --- Time entries --------------------------------------------------------------------------


class TimeEntryRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    timesheet_id: uuid.UUID
    timesheet_status: TimesheetStatus
    entry_date: date
    minutes: int
    client_id: uuid.UUID | None
    client_code: str | None
    client_name: str | None
    job_id: uuid.UUID | None
    job_number: str | None
    job_name: str | None
    task_id: uuid.UUID | None = Field(
        default=None, description="The task of the job the time was spent on; null without one"
    )
    task_name: str | None = Field(default=None, description="The task's title")
    service_id: uuid.UUID | None = Field(
        default=None, description="The service of the entry's job (read-only); null without a job"
    )
    service_name: str | None = None
    activity_code_id: uuid.UUID | None
    activity_code: str | None
    activity_name: str | None
    description: str | None
    internal_note: str | None = Field(
        default=None,
        description=(
            "Internal note; null (hidden) unless the caller owns the entry or holds "
            "time.read_all or time.manage_all"
        ),
    )
    is_billable: bool
    charge_rate: int = Field(description="Snapshot, minor units per hour")
    charge_value: int = Field(description="minutes x charge_rate / 60, rounded half up")
    cost_rate: int | None = Field(description="Null without rate.read")
    cost_value: int | None = Field(description="Null without rate.read")
    rate_source: str | None = Field(
        default=None,
        description=(
            "Level of the rate ladder that priced the entry: client_service_staff, "
            "client_service, client_staff, client, job, service_grade, staff, grade or default; "
            "null for time recorded before the ladder existed"
        ),
    )
    billing_status: BillingStatus
    is_locked: bool = Field(
        description="True when the sheet is submitted/approved or the entry is not unbilled"
    )
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_view(
        cls, view: EntryView, *, show_cost: bool, show_note: bool = False
    ) -> "TimeEntryRead":
        e = view.entry
        return cls(
            id=e.id,
            user_id=e.user_id,
            timesheet_id=e.timesheet_id,
            timesheet_status=view.timesheet_status,
            entry_date=e.entry_date,
            minutes=e.minutes,
            client_id=e.client_id,
            client_code=view.client_code,
            client_name=view.client_name,
            job_id=e.job_id,
            job_number=view.job_number,
            job_name=view.job_name,
            task_id=e.task_id,
            task_name=view.task_name,
            service_id=view.service_id,
            service_name=view.service_name,
            activity_code_id=e.activity_code_id,
            activity_code=view.activity_code,
            activity_name=view.activity_name,
            description=e.description,
            internal_note=e.internal_note if show_note else None,
            is_billable=e.is_billable,
            charge_rate=e.charge_rate,
            charge_value=e.charge_value,
            cost_rate=e.cost_rate if show_cost else None,
            cost_value=e.cost_value if show_cost else None,
            rate_source=e.rate_source,
            billing_status=e.billing_status,
            is_locked=(
                view.timesheet_status not in EDITABLE_SHEET_STATUSES
                or e.billing_status != "unbilled"
            ),
            created_by=e.created_by,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


class TimeEntryCreate(RequestModel):
    user_id: uuid.UUID | None = Field(
        default=None, description="Whose time; default the caller (others: time.manage_all)"
    )
    entry_date: EntryDate
    minutes: EntryMinutes = Field(description="Rounded up to the practice time increment")
    client_id: uuid.UUID | None = Field(default=None, description="Default: the job's client")
    job_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = Field(
        default=None, description="A task of the job (needs job_id); 422 for another job's task"
    )
    activity_code_id: uuid.UUID | None = None
    description: Description | None = None
    internal_note: InternalNote | None = Field(default=None, description=NOTE_DESCRIPTION)
    is_billable: bool | None = Field(
        default=None,
        description=(
            "Default false for non_billable jobs, non-chargeable activity codes and entries "
            "with no client; true otherwise"
        ),
    )

    _note = field_validator("internal_note")(blank_note_to_none)


class TimeEntryUpdate(RequestModel):
    """Every field optional. ``client_id``/``job_id``/``task_id``/``activity_code_id`` accept null.
    ``is_billable: null`` (or leaving it out while changing the client, job or activity)
    re-applies the default. A task belongs to one job: moving the entry to another job clears the
    task unless a task of the new job is sent."""

    entry_date: EntryDate | None = None
    minutes: EntryMinutes | None = None
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    activity_code_id: uuid.UUID | None = None
    description: Description | None = None
    internal_note: InternalNote | None = Field(default=None, description=NOTE_DESCRIPTION)
    is_billable: bool | None = None

    _note = field_validator("internal_note")(blank_note_to_none)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for field in ("entry_date", "minutes"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field}: This field cannot be null")
        return self


class TimeEntryBulkCreate(RequestModel):
    entries: list[TimeEntryCreate] = Field(min_length=1, max_length=100)


class WeekGridRow(BaseModel):
    client_id: uuid.UUID | None
    client_code: str | None
    client_name: str | None
    job_id: uuid.UUID | None
    job_number: str | None
    job_name: str | None
    task_id: uuid.UUID | None = None
    task_name: str | None = None
    activity_code_id: uuid.UUID | None
    activity_code: str | None
    activity_name: str | None
    is_billable: bool
    minutes: list[int] = Field(description="Minutes per day, one per day of the week")
    total_minutes: int
    entry_ids: list[uuid.UUID]


class WeekTargets(BaseModel):
    daily_minutes: dict[date, int] = Field(
        description="Target minutes for each day of the week (0 on a day off)"
    )
    weekly_minutes: int = Field(description="The week's target: the sum of the daily targets")


class WeekGrid(BaseModel):
    user_id: uuid.UUID
    week_start: date
    week_end: date
    days: list[date]
    timesheet_id: uuid.UUID | None
    timesheet_status: TimesheetStatus = Field(description="'open' when no sheet exists yet")
    rows: list[WeekGridRow]
    day_totals: list[int]
    total_minutes: int
    chargeable_minutes: int = Field(description="Billable minutes")
    non_chargeable_minutes: int
    today: date | None = Field(default=None, description="Today in the practice time zone")
    targets: WeekTargets | None = Field(
        default=None, description="The person's daily and weekly time targets"
    )
    working_days: list[date] = Field(
        default_factory=list, description="The days of the week that are the person's working days"
    )
    missing_days: list[date] = Field(
        default_factory=list,
        description="Working days up to today (inclusive) with no time logged",
    )
    day_variance: list[int] = Field(
        default_factory=list,
        description="Minutes logged minus the day's target, one per day of the week",
    )


class RevalueRequest(RequestModel):
    date_from: EntryDate
    date_to: EntryDate
    user_id: uuid.UUID | None = None
    client_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if self.date_from > self.date_to:
            raise ValueError("date_to: The range cannot end before it starts")
        if (self.date_to - self.date_from).days > 366 * 5:
            raise ValueError("date_to: The range can span at most five years")
        return self


class RevalueResult(BaseModel):
    entries_examined: int
    entries_changed: int
    charge_value_before: int
    charge_value_after: int
    value_delta: int = Field(description="Change in the charge value of the entries")
    cost_value_delta: int | None = Field(description="Null without rate.read")


# --- Timer ---------------------------------------------------------------------------------


class TimerRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    client_id: uuid.UUID | None
    job_id: uuid.UUID | None
    task_id: uuid.UUID | None = None
    activity_code_id: uuid.UUID | None
    description: str | None
    is_billable: bool
    is_running: bool
    started_at: datetime | None = Field(description="When the current run began; null paused")
    accumulated_seconds: int = Field(description="Seconds from earlier runs")
    elapsed_seconds: int
    started_on: date = Field(description="The day the timer was started (practice time zone)")
    created_at: datetime

    @classmethod
    def build(cls, timer: Timer, elapsed_seconds: int, started_on: date) -> "TimerRead":
        return cls(
            id=timer.id,
            user_id=timer.user_id,
            client_id=timer.client_id,
            job_id=timer.job_id,
            task_id=timer.task_id,
            activity_code_id=timer.activity_code_id,
            description=timer.description,
            is_billable=timer.is_billable,
            is_running=timer.started_at is not None,
            started_at=timer.started_at,
            accumulated_seconds=timer.accumulated_seconds,
            elapsed_seconds=elapsed_seconds,
            started_on=started_on,
            created_at=timer.created_at,
        )


class TimerStart(RequestModel):
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = Field(
        default=None, description="A task of the job (needs job_id); the entry on stop carries it"
    )
    activity_code_id: uuid.UUID | None = None
    description: Description | None = None
    is_billable: bool | None = None
    replace: bool = Field(
        default=False, description="Stop the running timer (writing its entry) first"
    )


class TimerStop(RequestModel):
    description: Description | None = Field(
        default=None, description="Replaces the timer's description on the entry"
    )


# --- Timesheets ----------------------------------------------------------------------------


class TimesheetRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    user_name: str | None
    week_start: date
    week_end: date
    status: TimesheetStatus
    submitted_at: datetime | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    decision_note: str | None
    entry_count: int
    total_minutes: int
    billable_minutes: int
    non_billable_minutes: int
    billable_value: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def build(cls, sheet: Timesheet, totals: SheetTotals, user_name: str | None) -> "TimesheetRead":
        return cls(
            id=sheet.id,
            user_id=sheet.user_id,
            user_name=user_name,
            week_start=sheet.week_start,
            week_end=sheet.week_start + timedelta(days=6),
            status=sheet.status,
            submitted_at=sheet.submitted_at,
            decided_by=sheet.decided_by,
            decided_at=sheet.decided_at,
            decision_note=sheet.decision_note,
            entry_count=totals.entry_count,
            total_minutes=totals.total_minutes,
            billable_minutes=totals.billable_minutes,
            non_billable_minutes=totals.total_minutes - totals.billable_minutes,
            billable_value=totals.billable_value,
            created_at=sheet.created_at,
            updated_at=sheet.updated_at,
        )


class DayTotal(BaseModel):
    day: date
    minutes: int


class TimesheetDetail(TimesheetRead):
    entries: list[TimeEntryRead]
    day_totals: list[DayTotal]


class TimesheetReject(RequestModel):
    decision_note: str = Field(min_length=1, max_length=2000)


class TimesheetApprove(RequestModel):
    decision_note: str | None = Field(default=None, max_length=2000)


class BulkApprove(RequestModel):
    timesheet_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
