"""Request and response models of the period-lock endpoints."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from app.db.models import PeriodLockEvent
from app.db.models.period_locks import EventType
from app.schemas.common import RequestModel

# Nothing earlier than the practice's rate epoch is ever worth locking.
EARLIEST_LOCK_DATE = date(2000, 1, 1)


def _blank_to_none(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


class PeriodLockEventRead(BaseModel):
    """One lock or reopening in the history."""

    id: uuid.UUID = Field(description="Id of the event")
    event_type: EventType = Field(description="`locked` (the lock moved forward) or `reopened`")
    previous_locked_through: date | None = Field(
        description="The last locked day before the event; null when nothing was locked"
    )
    locked_through: date | None = Field(
        description="The last locked day after the event; null when nothing is locked any more"
    )
    reason: str | None = Field(
        description="Why: optional for a lock, always present for a reopening"
    )
    actor_user_id: uuid.UUID = Field(description="The person who locked or reopened")
    actor_name: str | None = Field(description="Full name of that person")
    created_at: datetime = Field(description="When it happened (UTC)")

    @classmethod
    def from_model(cls, event: PeriodLockEvent, actor_name: str | None) -> "PeriodLockEventRead":
        return cls(
            id=event.id,
            event_type=event.event_type,
            previous_locked_through=event.previous_locked_through,
            locked_through=event.locked_through,
            reason=event.reason,
            actor_user_id=event.actor_user_id,
            actor_name=actor_name,
            created_at=event.created_at,
        )


class PeriodLockRead(BaseModel):
    """The practice's current lock."""

    locked_through: date | None = Field(
        description=(
            "The last locked day (inclusive): time, expenses and WIP dated on or before it "
            "cannot be created, edited or deleted. Null when nothing is locked"
        )
    )
    updated_at: datetime | None = Field(
        description="When the lock last changed (UTC); null when it never has"
    )
    last_event: PeriodLockEventRead | None = Field(
        description="The most recent lock or reopening; null when it never changed"
    )


class PeriodLockCreate(RequestModel):
    locked_through: date = Field(
        ge=EARLIEST_LOCK_DATE,
        description=(
            "The last day to lock (inclusive), a date in the practice's time zone. It must be "
            "later than the current lock (422 `PERIOD_LOCK_NOT_FORWARD`) and not after today "
            "(422 `PERIOD_LOCK_IN_FUTURE`)"
        ),
    )
    reason: str | None = Field(
        default=None, max_length=500, description="Optional note kept in the history"
    )

    _blank = field_validator("reason", mode="before")(_blank_to_none)


class PeriodLockReopen(RequestModel):
    reopen_to: date | None = Field(
        ge=EARLIEST_LOCK_DATE,
        description=(
            "The new last locked day: it must be earlier than the current lock (422 "
            "`PERIOD_LOCK_NOT_BACKWARD`). `null` removes the lock altogether. The field itself is "
            "required, so an empty body never unlocks the practice"
        ),
    )
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="Why the period is reopened. Required: 422 `PERIOD_LOCK_REASON_REQUIRED`",
    )

    _blank = field_validator("reason", mode="before")(_blank_to_none)
