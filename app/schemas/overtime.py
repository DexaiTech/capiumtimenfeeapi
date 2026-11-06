"""Request and response models of staff working hours and overtime rates (APT81, phase A)."""

import uuid
from datetime import date, datetime
from typing import Literal, Self, cast

from pydantic import BaseModel, Field, model_validator

from app.db.models import StaffHoursBasis
from app.repositories.overtime import OvertimeRateView
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
