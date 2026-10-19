"""Models of the day and calendar views, copying, corrections, missing timesheets and reminders."""

import datetime as dt
import uuid
from typing import Any, Literal, Self

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_validator

from app.schemas.common import RequestModel
from app.schemas.time import (
    NOTE_DESCRIPTION,
    Description,
    EntryDate,
    EntryMinutes,
    InternalNote,
    TimeEntryRead,
    TimerRead,
    TimesheetStatus,
    blank_note_to_none,
)

MAX_CALENDAR_DAYS = 62
MAX_COPY_ENTRIES = 500

# --- Day and calendar ----------------------------------------------------------------------


class TimeDay(BaseModel):
    user_id: uuid.UUID
    date: dt.date
    week_start: dt.date
    timesheet_id: uuid.UUID | None
    timesheet_status: TimesheetStatus = Field(description="'open' when no sheet exists yet")
    is_working_day: bool = Field(description="A working day of the person (not a day off)")
    entries: list[TimeEntryRead]
    total_minutes: int
    billable_minutes: int
    non_billable_minutes: int
    target_minutes: int = Field(description="The day's target; 0 on a day off")
    remaining_minutes: int = Field(description="Target minus logged, never below 0")
    variance_minutes: int = Field(description="Logged minus target")
    timer: TimerRead | None = Field(
        description="The caller's own timer when they view their own day; null otherwise"
    )


class CalendarDay(BaseModel):
    date: dt.date
    week_start: dt.date
    entry_count: int
    total_minutes: int
    billable_minutes: int
    target_minutes: int
    is_working_day: bool
    timesheet_status: TimesheetStatus = Field(
        description="Status of the timesheet of the day's week ('open' when none exists)"
    )


class TimeCalendar(BaseModel):
    user_id: uuid.UUID
    date_from: dt.date
    date_to: dt.date
    days: list[CalendarDay]
    total_minutes: int
    billable_minutes: int
    target_minutes: int


# --- Copying -------------------------------------------------------------------------------


class CopyWeekRequest(RequestModel):
    from_week_start: EntryDate = Field(
        description="Any day of the week to copy from (moved back to the practice's week start)"
    )
    to_week_start: EntryDate = Field(
        description="Any day of the week to copy into; it must be another week"
    )
    user_id: uuid.UUID | None = Field(
        default=None, description="Whose week; default the caller (others: time.manage_all)"
    )
    entry_ids: list[uuid.UUID] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_COPY_ENTRIES,
        description="Copy only these entries of the source week; default all of them",
    )
    include_notes: bool = Field(default=False, description="Copy the internal notes too")


class CopySkipped(BaseModel):
    source_entry_id: uuid.UUID
    code: str = Field(description="Why it was not copied, e.g. JOB_CLOSED or DAY_LIMIT_EXCEEDED")
    message: str


class CopyWeekResult(BaseModel):
    from_week_start: dt.date
    to_week_start: dt.date
    created: list[TimeEntryRead]
    skipped: list[CopySkipped]


class CopyEntryRequest(RequestModel):
    date: EntryDate = Field(
        validation_alias=AliasChoices("date", "entry_date"),
        description="The day to copy the entry to",
    )
    include_notes: bool = Field(default=False, description="Copy the internal note too")


# --- Corrections ---------------------------------------------------------------------------


class TimeCorrection(RequestModel):
    """A correction of approved, unbilled time. ``reason`` is required; send the fields that
    change (at least one). ``date``/``billable`` may also be written ``entry_date``/
    ``is_billable``; ``client_id``/``job_id``/``activity_code_id`` accept null."""

    reason: str | None = Field(
        default=None,
        max_length=2000,
        description="Why the time is corrected (required, 1-2000 characters)",
    )
    entry_date: EntryDate | None = Field(
        default=None, validation_alias=AliasChoices("date", "entry_date")
    )
    minutes: EntryMinutes | None = None
    client_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    activity_code_id: uuid.UUID | None = None
    description: Description | None = None
    internal_note: InternalNote | None = Field(default=None, description=NOTE_DESCRIPTION)
    is_billable: bool | None = Field(
        default=None, validation_alias=AliasChoices("billable", "is_billable")
    )

    _note = field_validator("internal_note")(blank_note_to_none)

    @model_validator(mode="after")
    def _validate(self) -> Self:
        for field in ("entry_date", "minutes"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field}: This field cannot be null")
        return self


class TimeEntryHistoryRead(BaseModel):
    id: uuid.UUID
    time_entry_id: uuid.UUID
    user_id: uuid.UUID = Field(description="Whose time it is")
    user_name: str | None
    corrected_by: uuid.UUID
    corrected_by_name: str | None
    reason: str
    before: dict[str, Any] = Field(description="The entry before the correction")
    after: dict[str, Any] = Field(description="The entry after the correction")
    changes: dict[str, list[Any]] = Field(
        description="{field: [before, after]} for every field that changed"
    )
    created_at: dt.datetime


# --- Missing timesheets and reminders ------------------------------------------------------

MissingStatus = Literal["none", "open", "rejected"]


class MissingTimesheet(BaseModel):
    user_id: uuid.UUID
    name: str
    week_start: dt.date
    status: MissingStatus = Field(
        description="none: no timesheet for the week; open: not submitted; rejected: returned"
    )
    timesheet_id: uuid.UUID | None
    logged_minutes: int
    expected_minutes: int = Field(description="The person's target for the week")
    missing_days: list[dt.date] = Field(
        description="Working days up to today (inclusive) with no time logged"
    )
    last_reminded_at: dt.datetime | None = Field(
        description="When a timesheet reminder for this week was last sent"
    )
    has_login: bool = Field(
        description="False: nobody can sign in as them; they cannot be reminded"
    )


class RemindersRun(RequestModel):
    week_start: EntryDate | None = Field(
        default=None, description="Any day of the week; default: the last completed week"
    )


class RemindersResult(BaseModel):
    week_start: dt.date
    missing: int = Field(description="People whose timesheet for the week is missing")
    reminded: int = Field(description="People sent a timesheet reminder now")
    already_reminded: int = Field(description="People who were reminded about this week before")
    unreachable: int = Field(description="People who cannot be notified (no login, not active)")
    sheets_waiting: int = Field(description="Submitted timesheets of that week or earlier")
    approvers_reminded: int = Field(description="Approvers sent a reminder now")
    approvers_already_reminded: int = Field(description="Approvers reminded about this week before")
    disabled: list[str] = Field(
        description="Notification types the practice has switched off, so nothing was sent for them"
    )
