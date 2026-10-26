"""Request and response models of the period-lock endpoints."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.db.models import LockedPeriod, PeriodLockEvent
from app.db.models.period_locks import EventType, PeriodType
from app.schemas.common import RequestModel
from app.utils.lock_periods import period_label

# Nothing earlier than the practice's rate epoch is ever worth locking.
EARLIEST_LOCK_YEAR = 2000
LATEST_LOCK_YEAR = 2100


def _blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class PeriodLockEventRead(BaseModel):
    """One lock or unlock in the history."""

    id: uuid.UUID = Field(description="Id of the event")
    event_type: EventType = Field(description="`locked` or `reopened` (unlocked)")
    period_type: PeriodType | None = Field(
        description="`month`, `quarter`, `year` or `legacy`; null on events from before periods"
    )
    period_start: date | None = Field(description="First day of the period")
    period_end: date | None = Field(description="Last day of the period")
    period_label: str | None = Field(description="`March 2026`, `Q1 2026`, `2026`")
    previous_locked_through: date | None = Field(
        description="Events from before periods only: the single lock date before the event"
    )
    locked_through: date | None = Field(
        description="Events from before periods only: the single lock date after the event"
    )
    reason: str | None = Field(description="Why: optional for a lock, always present for an unlock")
    actor_user_id: uuid.UUID = Field(description="The person who locked or unlocked")
    actor_name: str | None = Field(description="Full name of that person")
    created_at: datetime = Field(description="When it happened (UTC)")

    @classmethod
    def from_model(cls, event: PeriodLockEvent, actor_name: str | None) -> "PeriodLockEventRead":
        label = (
            period_label(event.period_type, event.period_start, event.period_end)
            if event.period_type and event.period_start and event.period_end
            else None
        )
        return cls(
            id=event.id,
            event_type=event.event_type,
            period_type=event.period_type,
            period_start=event.period_start,
            period_end=event.period_end,
            period_label=label,
            previous_locked_through=event.previous_locked_through,
            locked_through=event.locked_through,
            reason=event.reason,
            actor_user_id=event.actor_user_id,
            actor_name=actor_name,
            created_at=event.created_at,
        )


class LockedPeriodRead(BaseModel):
    """One locked month, quarter or year."""

    id: uuid.UUID = Field(description="Id of the lock: pass it to unlock the period")
    period_type: PeriodType = Field(
        description="`month`, `quarter` or `year`; `legacy` is a lock set through a date before "
        "periods existed (it can be unlocked, not created)"
    )
    start_date: date = Field(description="First locked day")
    end_date: date = Field(description="Last locked day (inclusive)")
    label: str = Field(description="`March 2026`, `Q1 2026`, `2026`")
    reason: str | None = Field(description="Note given when it was locked")
    locked_by: uuid.UUID | None = Field(description="Who locked it")
    locked_by_name: str | None = Field(description="Full name of that person")
    locked_at: datetime = Field(description="When it was locked (UTC)")

    @classmethod
    def from_model(cls, period: LockedPeriod, name: str | None) -> "LockedPeriodRead":
        return cls(
            id=period.id,
            period_type=period.period_type,
            start_date=period.start_date,
            end_date=period.end_date,
            label=period_label(period.period_type, period.start_date, period.end_date),
            reason=period.reason,
            locked_by=period.locked_by,
            locked_by_name=name,
            locked_at=period.locked_at,
        )


class PeriodLockRead(BaseModel):
    """The practice's current locks."""

    periods: list[LockedPeriodRead] = Field(
        description=(
            "The locked periods, oldest first. Time, expenses and WIP dated inside any of them "
            "cannot be created, edited or deleted. Empty when nothing is locked"
        )
    )
    last_event: PeriodLockEventRead | None = Field(
        description="The most recent lock or unlock; null when it never changed"
    )


class PeriodLockCreate(RequestModel):
    period_type: Literal["month", "quarter", "year"] = Field(
        description="What to lock: a calendar `month`, `quarter` or `year`"
    )
    year: int = Field(ge=EARLIEST_LOCK_YEAR, le=LATEST_LOCK_YEAR, description="The calendar year")
    period: int | None = Field(
        default=None,
        ge=1,
        le=12,
        description="The month (1-12) or quarter (1-4); omitted for a year",
    )
    reason: str | None = Field(
        default=None, max_length=500, description="Optional note kept in the history"
    )

    _blank = field_validator("reason", mode="before")(_blank_to_none)


class PeriodLockReopen(RequestModel):
    lock_id: uuid.UUID = Field(description="The locked period to unlock (`periods[].id`)")
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="Why the period is unlocked. Required: 422 `PERIOD_LOCK_REASON_REQUIRED`",
    )

    _blank = field_validator("reason", mode="before")(_blank_to_none)
