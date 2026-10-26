"""Time recording: weekly timesheets, time entries and running timers."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin
from app.db.models.clients import _member_fk

TIMESHEET_STATUSES = ("open", "submitted", "approved", "rejected")
# Shared by time entries and disbursements.
BILLING_STATUSES = ("unbilled", "in_draft", "billed", "written_off")


class Timesheet(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """One person's week. Entries are locked while it is submitted or approved."""

    __tablename__ = "timesheets"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_timesheets_organization_id_id"),
        UniqueConstraint(
            "organization_id",
            "user_id",
            "week_start",
            name="uq_timesheets_organization_id_user_id_week_start",
        ),
        _member_fk("user_id", "timesheets"),
        _member_fk("decided_by", "timesheets"),
        CheckConstraint("status IN ('open', 'submitted', 'approved', 'rejected')", name="status"),
        Index(
            "ix_timesheets_organization_id_status_week_start",
            "organization_id",
            "status",
            "week_start",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    # The person's time needs no approval (practice setting + permission ``timesheet.exempt``):
    # the sheet counts as approved for WIP and billing without being submitted.
    approval_exempt: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class TimeEntry(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """Time spent by one person on one day.

    ``charge_rate``/``cost_rate`` are snapshots taken when the entry is saved (and re-taken
    on every change while it is unbilled); ``charge_value``/``cost_value`` are
    ``minutes x rate / 60`` rounded half up. A billable entry is work in progress (WIP) while
    ``billing_status = 'unbilled'``.
    """

    __tablename__ = "time_entries"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_time_entries_organization_id_id"),
        _member_fk("user_id", "time_entries"),
        ForeignKeyConstraint(
            ["organization_id", "timesheet_id"],
            ["timesheets.organization_id", "timesheets.id"],
            name="fk_time_entries_organization_id_timesheet_id_timesheets",
        ),
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_time_entries_organization_id_client_id_clients",
        ),
        # The job must belong to the entry's client.
        ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_time_entries_organization_id_client_id_job_id_jobs",
        ),
        # The task must belong to the entry's job.
        ForeignKeyConstraint(
            ["organization_id", "job_id", "task_id"],
            ["tasks.organization_id", "tasks.job_id", "tasks.id"],
            name="fk_time_entries_organization_id_job_id_task_id_tasks",
        ),
        ForeignKeyConstraint(
            ["organization_id", "activity_code_id"],
            ["activity_codes.organization_id", "activity_codes.id"],
            name="fk_time_entries_organization_id_activity_code_id_activity_codes",
        ),
        CheckConstraint("minutes BETWEEN 1 AND 1440", name="minutes"),
        CheckConstraint(
            "internal_note IS NULL OR length(internal_note) <= 2000", name="internal_note_length"
        ),
        CheckConstraint("job_id IS NULL OR client_id IS NOT NULL", name="job_needs_client"),
        CheckConstraint("task_id IS NULL OR job_id IS NOT NULL", name="task_needs_job"),
        CheckConstraint("NOT is_billable OR client_id IS NOT NULL", name="billable_needs_client"),
        CheckConstraint(
            "billing_status IN ('unbilled', 'in_draft', 'billed', 'written_off')",
            name="billing_status",
        ),
        CheckConstraint("is_billable OR billing_status = 'unbilled'", name="non_billable_status"),
        CheckConstraint(
            "charge_rate >= 0 AND cost_rate >= 0 AND charge_value >= 0 AND cost_value >= 0",
            name="amounts",
        ),
        CheckConstraint(
            "rate_source IS NULL OR rate_source IN ('client_service_staff', 'client_service', "
            "'client_staff', 'client', 'job', 'service_grade', 'staff', 'grade', 'default')",
            name="rate_source",
        ),
        Index(
            "ix_time_entries_organization_id_user_id_entry_date",
            "organization_id",
            "user_id",
            "entry_date",
        ),
        Index(
            "ix_time_entries_organization_id_client_id_entry_date",
            "organization_id",
            "client_id",
            "entry_date",
        ),
        Index("ix_time_entries_organization_id_job_id", "organization_id", "job_id"),
        Index(
            "ix_time_entries_organization_id_task_id",
            "organization_id",
            "task_id",
            postgresql_where=text("task_id IS NOT NULL"),
        ),
        Index("ix_time_entries_timesheet_id", "timesheet_id"),
        # The WIP ledger: billable time not yet billed.
        Index(
            "ix_time_entries_wip",
            "organization_id",
            "client_id",
            "entry_date",
            postgresql_where=text("is_billable AND billing_status = 'unbilled'"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    timesheet_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # The task of the job the time was spent on (optional; a task has one job).
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    activity_code_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    description: Mapped[str | None] = mapped_column(Text)
    # Never printed on an invoice; seen by the owner and people who read or manage all time.
    internal_note: Mapped[str | None] = mapped_column(Text)
    is_billable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    charge_rate: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    cost_rate: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    charge_value: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    cost_value: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    # Which level of the rate ladder priced the entry (see ``RATE_SOURCES``); null for time
    # recorded before the ladder existed.
    rate_source: Mapped[str | None] = mapped_column(Text)
    billing_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="unbilled")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    # Set by the opening-balance import so historic time can be shown apart from new time; it
    # otherwise behaves exactly like any other entry (same WIP and billing allocation).
    is_opening_balance: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class Timer(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A running stopwatch. At most one per person per practice; stopping it writes a
    time entry and deletes the timer."""

    __tablename__ = "timers"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_timers_organization_id_user_id"),
        _member_fk("user_id", "timers"),
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_timers_organization_id_client_id_clients",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_timers_organization_id_client_id_job_id_jobs",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "job_id", "task_id"],
            ["tasks.organization_id", "tasks.job_id", "tasks.id"],
            name="fk_timers_organization_id_job_id_task_id_tasks",
        ),
        ForeignKeyConstraint(
            ["organization_id", "activity_code_id"],
            ["activity_codes.organization_id", "activity_codes.id"],
            name="fk_timers_organization_id_activity_code_id_activity_codes",
        ),
        CheckConstraint("job_id IS NULL OR client_id IS NOT NULL", name="job_needs_client"),
        CheckConstraint("task_id IS NULL OR job_id IS NOT NULL", name="task_needs_job"),
        CheckConstraint("accumulated_seconds >= 0", name="accumulated_seconds"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    activity_code_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    description: Mapped[str | None] = mapped_column(Text)
    is_billable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Null while paused. Elapsed = accumulated_seconds + (now - started_at).
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accumulated_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )


__all__ = ["TimeEntry", "Timer", "Timesheet"]
