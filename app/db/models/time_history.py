"""Correction history of approved time and the log of timesheet reminders.

Both tables keep the ids they name (time entry, people) without foreign keys, like
``rate_changes``: the history outlives an entry that is deleted later, and it never blocks
deleting or merging a staff member.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import CheckConstraint, Date, DateTime, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin

REMINDER_KINDS = ("timesheet", "approval")


class TimeEntryHistory(Base, IdMixin, OrgScopedMixin):
    """One correction of approved, unbilled time: who, why, and the entry before and after.

    Append-only: a trigger (migration ``023``) refuses every UPDATE and DELETE. ``before`` and
    ``after`` are complete snapshots of the entry's fields, so a row can be read on its own.
    """

    __tablename__ = "time_entry_history"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_time_entry_history_organization_id_id"),
        CheckConstraint("length(btrim(reason)) BETWEEN 1 AND 2000", name="reason_length"),
        CheckConstraint(
            "jsonb_typeof(before) = 'object' AND jsonb_typeof(after) = 'object'",
            name="snapshots_are_objects",
        ),
        Index(
            "ix_time_entry_history_organization_id_time_entry_id_created_at",
            "organization_id",
            "time_entry_id",
            "created_at",
        ),
    )

    time_entry_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The person whose time it is, and the person who corrected it.
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    corrected_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    before: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    after: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # The moment of the insert (not of the transaction), so the order of corrections of an
    # entry is the order in which they took the entry's lock.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class TimesheetReminder(Base, IdMixin, OrgScopedMixin):
    """A reminder sent to a person about a week: ``timesheet`` (their timesheet is missing, open
    or returned) or ``approval`` (timesheets are waiting for them to approve). At most one per
    person, week and kind: that is what makes a reminder run repeatable."""

    __tablename__ = "timesheet_reminders"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_id",
            "week_start",
            "kind",
            name="uq_timesheet_reminders_organization_id_user_id_week_start_kind",
        ),
        CheckConstraint("kind IN ('timesheet', 'approval')", name="kind"),
        Index(
            "ix_timesheet_reminders_organization_id_week_start_kind",
            "organization_id",
            "week_start",
            "kind",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    reminded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["REMINDER_KINDS", "TimeEntryHistory", "TimesheetReminder"]
