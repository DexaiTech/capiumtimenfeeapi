"""Period locks: the day up to which a practice has closed its time, expenses and WIP.

* ``period_locks``: one row per practice with the current state. ``locked_through`` is the last
  locked day (inclusive); null means nothing is locked. Every write that creates, edits or
  deletes dated time, expense or WIP data reads it ``FOR SHARE`` (``app/services/period_locks.py``)
  and a lock or a reopening takes it ``FOR UPDATE``, so a change of the lock and a write checked
  against the old state never overlap.
* ``period_lock_events``: the append-only history. One row per lock (``locked``) and per
  reopening (``reopened``), with the dates before and after, the reason and who did it. A
  trigger refuses every UPDATE and every direct DELETE; the rows go only with their practice.
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


class PeriodLock(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """The current lock of a practice (one row each)."""

    __tablename__ = "period_locks"
    __table_args__ = (
        UniqueConstraint("organization_id", name="uq_period_locks_organization_id"),
        UniqueConstraint("organization_id", "id", name="uq_period_locks_organization_id_id"),
    )

    locked_through: Mapped[date | None] = mapped_column(Date)


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
            "event_type <> 'locked' OR (locked_through IS NOT NULL AND "
            "(previous_locked_through IS NULL OR locked_through > previous_locked_through))",
            name="locked_moves_forward",
        ),
        CheckConstraint(
            "event_type <> 'reopened' OR (previous_locked_through IS NOT NULL AND "
            "(locked_through IS NULL OR locked_through < previous_locked_through))",
            name="reopened_moves_back",
        ),
        Index(
            "ix_period_lock_events_organization_id_created_at",
            "organization_id",
            "created_at",
            "id",
        ),
    )

    event_type: Mapped[EventType] = mapped_column(Text, nullable=False)
    # The lock before and after this event; null = nothing was (is) locked.
    previous_locked_through: Mapped[date | None] = mapped_column(Date)
    locked_through: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(Text)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # clock_timestamp(), not now(): an event written after waiting for the lock must sort after
    # the event it waited for, although its transaction started earlier.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )


__all__ = ["EventType", "PeriodLock", "PeriodLockEvent"]
