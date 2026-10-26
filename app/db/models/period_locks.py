"""Period locks: the day up to which a practice has closed its time, expenses and WIP.

* ``locked_periods``: one row per locked month, quarter or calendar year (``legacy`` rows hold a
  lock that was set through a free date before periods existed). A day is locked when it lies in
  any row. Unlocking a period deletes its row.
* ``period_locks``: one row per practice, kept as the serialisation point. Every write that
  creates, edits or deletes dated time, expense or WIP data reads it ``FOR SHARE``
  (``app/services/period_locks.py``) and a lock or an unlock takes it ``FOR UPDATE``, so a change
  of the locks and a write checked against the old state never overlap. ``locked_through`` is
  unused since migration 052.
* ``period_lock_events``: the append-only history. One row per lock (``locked``) and per
  reopening (``reopened``), with the period, the reason and who did it. A trigger refuses every
  UPDATE and every direct DELETE; the rows go only with their practice.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin

EventType = Literal["locked", "reopened"]
PeriodType = Literal["month", "quarter", "year", "legacy"]


class PeriodLock(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """The current lock of a practice (one row each)."""

    __tablename__ = "period_locks"
    __table_args__ = (
        UniqueConstraint("organization_id", name="uq_period_locks_organization_id"),
        UniqueConstraint("organization_id", "id", name="uq_period_locks_organization_id_id"),
    )

    locked_through: Mapped[date | None] = mapped_column(Date)


class LockedPeriod(Base, IdMixin, OrgScopedMixin):
    """A locked month, quarter or calendar year (both ends inclusive)."""

    __tablename__ = "locked_periods"
    __table_args__ = (
        CheckConstraint(
            "period_type IN ('month', 'quarter', 'year', 'legacy')", name="period_type"
        ),
        CheckConstraint("start_date <= end_date", name="range"),
        UniqueConstraint(
            "organization_id",
            "period_type",
            "start_date",
            name="uq_locked_periods_organization_id_period_type_start_date",
        ),
        Index(
            "ix_locked_periods_organization_id_dates", "organization_id", "start_date", "end_date"
        ),
    )

    period_type: Mapped[PeriodType] = mapped_column(Text, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    locked_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PeriodLockEvent(Base, IdMixin, OrgScopedMixin):
    """One lock or reopening. Never updated, never deleted."""

    __tablename__ = "period_lock_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_period_lock_events_organization_id_actor_members",
        ),
        CheckConstraint("event_type IN ('locked', 'reopened')", name="event_type"),
        CheckConstraint("reason IS NULL OR length(reason) BETWEEN 1 AND 500", name="reason"),
        CheckConstraint("event_type <> 'reopened' OR reason IS NOT NULL", name="reopen_reason"),
        CheckConstraint(
            "period_type IS NULL OR (period_type IN ('month', 'quarter', 'year', 'legacy') "
            "AND period_start IS NOT NULL AND period_end IS NOT NULL)",
            name="period",
        ),
        Index(
            "ix_period_lock_events_organization_id_created_at",
            "organization_id",
            "created_at",
            "id",
        ),
    )

    event_type: Mapped[EventType] = mapped_column(Text, nullable=False)
    # The period the event is about; null on the events written before periods existed.
    period_type: Mapped[PeriodType | None] = mapped_column(Text)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    # Events before periods existed: the single lock date before and after (null = none).
    previous_locked_through: Mapped[date | None] = mapped_column(Date)
    locked_through: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(Text)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # clock_timestamp(), not now(): an event written after waiting for the lock must sort after
    # the event it waited for, although its transaction started earlier.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )


__all__ = ["EventType", "LockedPeriod", "PeriodLock", "PeriodLockEvent", "PeriodType"]
