"""Request and response models of staff working hours and overtime rates (APT81, phase A)."""

import uuid
from datetime import date, datetime
from typing import Literal, Self, cast

from pydantic import BaseModel, Field, model_validator

from app.db.models import StaffHoursBasis
from app.repositories.overtime import OvertimeRateView, OvertimeView
from app.schemas.common import RequestModel
from app.schemas.rate_rules import RefRead, RuleStatus, StaffRef, rule_status
from app.schemas.settings import Money

HoursBasis = Literal["weekly", "monthly"]
OvertimeLevel = Literal["grade", "staff"]
OvertimeMode = Literal["multiplier", "fixed"]

# The most minutes a week (7 x 24 h) and a month (31 x 24 h) can hold.
WEEK_MINUTES = 7 * 24 * 60
MONTH_MINUTES = 31 * 24 * 60

_PERIOD_STATUS = "current, future or ended, as of today in the practice time zone"


# --- Working hours ----------------------------------------------------------------------


class StaffHoursRead(BaseModel):
    id: uuid.UUID = Field(description="The hours period's id")
    user_id: uuid.UUID = Field(description="The staff member (their user id)")
    basis: HoursBasis = Field(description="`weekly` or `monthly`: the period the hours are for")
    standard_minutes: int = Field(
        description="Standard working minutes in one week or month; beyond them is overtime"
    )
    effective_from: date = Field(description="First day the hours apply")
    effective_to: date | None = Field(description="Last day (inclusive); null = open-ended")
    status: RuleStatus = Field(description=_PERIOD_STATUS)
    created_by: uuid.UUID | None = Field(description="Who added it; null for migrated rows")
    created_at: datetime = Field(description="When it was added")
    updated_at: datetime = Field(description="When it last changed")

    @classmethod
    def build(cls, row: StaffHoursBasis, *, today: date) -> "StaffHoursRead":
        return cls(
            id=row.id,
            user_id=row.user_id,
            basis=cast(HoursBasis, row.basis),
            standard_minutes=row.standard_minutes,
            effective_from=row.effective_from,
            effective_to=row.effective_to,
            status=rule_status(row.effective_from, row.effective_to, today),
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class StaffHoursCreate(RequestModel):
    basis: HoursBasis = Field(description="`weekly` or `monthly`")
    standard_minutes: int = Field(
        ge=1,
        le=MONTH_MINUTES,
        description=(
            f"Standard working minutes in the period: 1 to {WEEK_MINUTES} a week, "
            f"1 to {MONTH_MINUTES} a month (2250 = 37.5 hours)"
        ),
    )
    effective_from: date = Field(
        description=(
            "First day the hours apply; the person's open-ended hours that started earlier end "
            "the day before"
        )
    )

    @model_validator(mode="after")
    def _week(self) -> Self:
        if self.basis == "weekly" and self.standard_minutes > WEEK_MINUTES:
            raise ValueError(
                f"standard_minutes: A week has at most {WEEK_MINUTES} minutes (168 hours)"
            )
        return self


# --- Overtime rates -----------------------------------------------------------------------


class OvertimeRateRead(BaseModel):
    id: uuid.UUID = Field(description="The overtime rate's id")
    level: OvertimeLevel = Field(
        description="`grade` or `staff` (a staff rate overrides its grade's)"
    )
    grade: RefRead | None = Field(description="The grade, for a grade rate")
    staff: StaffRef | None = Field(description="The staff member, for a staff rate")
    mode: OvertimeMode = Field(
        description="`multiplier` (times the standard cost rate) or `fixed` (pence per hour)"
    )
    multiplier_bp: int | None = Field(
        description="Basis points of the standard cost rate (15000 = 1.5x); multiplier mode only"
    )
    rate: int | None = Field(
        description="Pence per hour; fixed mode only. Null without `rate.read`"
    )
    effective_from: date = Field(description="First day the rate applies")
    effective_to: date | None = Field(description="Last day (inclusive); null = open-ended")
    status: RuleStatus = Field(description=_PERIOD_STATUS)
    created_by: uuid.UUID | None = Field(description="Who added it")
    created_at: datetime = Field(description="When it was added")
    updated_at: datetime = Field(description="When it last changed")

    @classmethod
    def from_view(
        cls, view: OvertimeRateView, *, today: date, show_cost: bool
    ) -> "OvertimeRateRead":
        rate = view.rate
        return cls(
            id=rate.id,
            level=cast(OvertimeLevel, rate.level),
            grade=RefRead(id=rate.grade_id, code=view.grade_code, name=view.grade_name)
            if rate.grade_id is not None and view.grade_name is not None
            else None,
            staff=StaffRef(id=rate.user_id, code=view.staff_code, full_name=view.user_name)
            if rate.user_id is not None and view.user_name
            else None,
            mode=cast(OvertimeMode, rate.mode),
            multiplier_bp=rate.multiplier_bp,
            rate=rate.rate if show_cost else None,
            effective_from=rate.effective_from,
            effective_to=rate.effective_to,
            status=rule_status(rate.effective_from, rate.effective_to, today),
            created_by=rate.created_by,
            created_at=rate.created_at,
            updated_at=rate.updated_at,
        )


class OvertimeRateCreate(RequestModel):
    level: OvertimeLevel = Field(description="`grade` or `staff`")
    grade_id: uuid.UUID | None = Field(default=None, description="The grade; level `grade` only")
    user_id: uuid.UUID | None = Field(
        default=None, description="The staff member; level `staff` only"
    )
    mode: OvertimeMode = Field(
        description="`multiplier` (times the standard cost rate) or `fixed` (pence per hour)"
    )
    multiplier_bp: int | None = Field(
        default=None,
        ge=10_000,
        le=100_000,
        description="Basis points, 10000 (1x) to 100000 (10x); mode `multiplier` only",
    )
    rate: Money | None = Field(default=None, description="Pence per hour; mode `fixed` only")
    effective_from: date = Field(description="First day the rate applies")
    effective_to: date | None = Field(
        default=None, description="Last day (inclusive); null = open-ended"
    )

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if self.level == "grade":
            if self.grade_id is None:
                raise ValueError("grade_id: Choose the grade")
            if self.user_id is not None:
                raise ValueError("user_id: A grade rate names no staff member")
        else:
            if self.user_id is None:
                raise ValueError("user_id: Choose the staff member")
            if self.grade_id is not None:
                raise ValueError("grade_id: A staff rate names no grade")
        if self.mode == "multiplier":
            if self.multiplier_bp is None:
                raise ValueError("multiplier_bp: Enter the multiplier")
            if self.rate is not None:
                raise ValueError("rate: A multiplier rate has no fixed amount")
        else:
            if self.rate is None:
                raise ValueError("rate: Enter the amount per hour")
            if self.multiplier_bp is not None:
                raise ValueError("multiplier_bp: A fixed rate has no multiplier")
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to: Must not be before effective_from")
        return self


class OvertimeRateEnd(RequestModel):
    effective_to: date = Field(description="The rate's last day (inclusive)")


class OvertimeResolveRead(BaseModel):
    user_id: uuid.UUID = Field(description="The staff member")
    on_date: date = Field(description="The day asked about")
    grade_id: uuid.UUID | None = Field(description="The staff member's grade; null when none")
    source: Literal["staff", "grade"] | None = Field(
        description="Where the rate came from: the staff member's own, their grade's, or null "
        "when neither has one that day"
    )
    rate: OvertimeRateRead | None = Field(description="The one rate that applies, or null")


# --- Overtime of time entries (phases B and C) ----------------------------------------------

OvertimeStatus = Literal["pending", "approved", "rejected"]
OvertimeSource = Literal["staff", "grade", "none"]
_NEEDS_COST = " Null without `report.staff_cost`"


class OvertimeUserRef(BaseModel):
    id: uuid.UUID = Field(description="The staff member (their user id)")
    full_name: str | None = Field(description="Their name")


class OvertimeJobRef(BaseModel):
    id: uuid.UUID = Field(description="The job's id")
    number: str | None = Field(description="The job's number")
    name: str | None = Field(description="The job's name")


class OvertimeClientRef(BaseModel):
    id: uuid.UUID = Field(description="The client's id")
    name: str | None = Field(description="The client's name")


class OvertimeRead(BaseModel):
    id: uuid.UUID = Field(description="The overtime row's id")
    time_entry_id: uuid.UUID = Field(description="The time entry it is the overtime portion of")
    user: OvertimeUserRef = Field(description="Whose time")
    entry_date: date = Field(description="The entry's day")
    entry_minutes: int = Field(description="The whole entry's minutes (standard + overtime)")
    description: str | None = Field(description="The entry's description")
    job: OvertimeJobRef | None = Field(description="The entry's job")
    client: OvertimeClientRef | None = Field(description="The entry's client")
    overtime_minutes: int = Field(description="The minutes of the entry beyond the standard hours")
    allocated: bool = Field(
        description="On a job of a client other than the built-in Internal client; false: "
        "unallocated (Internal client time)"
    )
    rate_source: OvertimeSource = Field(
        description="`staff` (the person's own overtime rate), `grade` (their grade's) or "
        "`none` (no overtime rate: rate and cost 0)"
    )
    overtime_rate_id: uuid.UUID | None = Field(
        description="The overtime rate applied; null when none (or since deleted)"
    )
    overtime_rate: int | None = Field(
        description="Pence per hour applied to the overtime minutes." + _NEEDS_COST
    )
    overtime_cost: int | None = Field(
        description="overtime_minutes at overtime_rate, in pence." + _NEEDS_COST
    )
    premium_cost: int | None = Field(
        description="overtime_cost less the standard cost of the same minutes (never below 0); "
        "rejecting the overtime removes it." + _NEEDS_COST
    )
    status: OvertimeStatus = Field(
        description="`pending` (unapproved), `approved` or `rejected` (only the premium is "
        "removed: the hours count at their standard cost)"
    )
    confirmed_by: uuid.UUID | None = Field(
        description="Who confirmed adding the overtime; null when it came from a re-split"
    )
    confirmed_at: datetime | None = Field(description="When it was confirmed")
    decided_by: uuid.UUID | None = Field(
        description="Who approved or rejected it; null while pending or when approved "
        "automatically (time that needs no approval)"
    )
    decided_by_name: str | None = Field(description="Their name")
    decided_at: datetime | None = Field(description="When it was decided")
    decision_note: str | None = Field(description="The note given with the decision")
    created_at: datetime = Field(description="When the overtime was first recorded")
    updated_at: datetime = Field(description="When it last changed")

    @classmethod
    def from_view(cls, view: OvertimeView, *, show_cost: bool) -> "OvertimeRead":
        row = view.row
        return cls(
            id=row.id,
            time_entry_id=row.time_entry_id,
            user=OvertimeUserRef(id=row.user_id, full_name=view.user_name),
            entry_date=view.entry_date,
            entry_minutes=view.entry_minutes,
            description=view.description,
            job=OvertimeJobRef(id=view.job_id, number=view.job_number, name=view.job_name)
            if view.job_id is not None
            else None,
            client=OvertimeClientRef(id=view.client_id, name=view.client_name)
            if view.client_id is not None
            else None,
            overtime_minutes=row.overtime_minutes,
            allocated=row.allocated,
            rate_source=cast(OvertimeSource, row.rate_source),
            overtime_rate_id=row.overtime_rate_id,
            overtime_rate=row.overtime_rate if show_cost else None,
            overtime_cost=row.overtime_cost if show_cost else None,
            premium_cost=row.premium_cost if show_cost else None,
            status=cast(OvertimeStatus, row.status),
            confirmed_by=row.confirmed_by,
            confirmed_at=row.confirmed_at,
            decided_by=row.decided_by,
            decided_by_name=view.decided_by_name,
            decided_at=row.decided_at,
            decision_note=row.decision_note,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class OvertimeReject(RequestModel):
    note: str = Field(
        min_length=1, max_length=2000, description="Why the overtime is rejected (required)"
    )


class OvertimeApprove(RequestModel):
    note: str | None = Field(default=None, max_length=2000, description="An optional note")
