"""Overtime set-up (ticket APT81, phase A): each person's dated standard hours and the dated
overtime rates of grades and staff.

* ``staff_hours_bases``: a person's hours basis (``weekly`` or ``monthly``) and standard minutes
  in that period, from ``effective_from`` to ``effective_to`` inclusive (open-ended when null).
  A person's periods never overlap (an exclusion constraint). The rows belong to the membership
  and go with it.
* ``overtime_rates``: the overtime cost rate of a grade or of one staff member, either a
  multiplier of the standard cost rate (``multiplier_bp``, 15000 = 1.5x) or a fixed amount in
  pence per hour (``rate``), with the same dated periods. The rates of one grade or one person
  never overlap. A staff rate overrides the grade rate; the two never stack.
* ``time_entry_overtime`` (phase B): the overtime portion of one time entry, worked out from the
  person's standard hours, with the overtime rate and cost applied, and its approval status. It
  is cost only: charge-out, WIP and invoices never read it.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin
from app.db.models.clients import _member_fk
from app.db.types import UTCDateTime, Uuid36

HOURS_BASES = ("weekly", "monthly")
OVERTIME_LEVELS = ("grade", "staff")
OVERTIME_MODES = ("multiplier", "fixed")
OVERTIME_SOURCES = ("staff", "grade", "none")
OVERTIME_STATUSES = ("pending", "approved", "rejected")


class StaffHoursBasis(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "staff_hours_bases"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_staff_hours_bases_organization_members",
            ondelete="CASCADE",
        ),
        CheckConstraint("basis IN ('weekly', 'monthly')", name="basis"),
        CheckConstraint("standard_minutes > 0", name="standard_minutes"),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="period"),
        # No two periods of one scope share a day: a trigger, app/db/triggers.py.
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid36(), nullable=False)
    basis: Mapped[str] = mapped_column(String(32), nullable=False)
    # Standard working minutes in one week or one month; beyond them is overtime.
    standard_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid36(), ForeignKey("users.id"))


class OvertimeRate(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "overtime_rates"
    __table_args__ = (
        _member_fk("user_id", "overtime_rates"),
        ForeignKeyConstraint(
            ["organization_id", "grade_id"],
            ["grades.organization_id", "grades.id"],
            name="fk_overtime_rates_organization_id_grade_id_grades",
        ),
        CheckConstraint("level IN ('grade', 'staff')", name="level"),
        CheckConstraint(
            "(level = 'grade' AND grade_id IS NOT NULL AND user_id IS NULL) "
            "OR (level = 'staff' AND user_id IS NOT NULL AND grade_id IS NULL)",
            name="scope",
        ),
        CheckConstraint("mode IN ('multiplier', 'fixed')", name="mode"),
        CheckConstraint(
            "(mode = 'multiplier' AND multiplier_bp BETWEEN 10000 AND 100000 AND rate IS NULL) "
            "OR (mode = 'fixed' AND rate >= 0 AND multiplier_bp IS NULL)",
            name="amount",
        ),
        CheckConstraint("effective_to IS NULL OR effective_to >= effective_from", name="period"),
        # No two periods of one scope share a day: a trigger, app/db/triggers.py.
    )

    level: Mapped[str] = mapped_column(String(32), nullable=False)
    grade_id: Mapped[uuid.UUID | None] = mapped_column(Uuid36())
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid36())
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    # Basis points of the standard cost rate (15000 = 1.5x); ``multiplier`` mode only.
    multiplier_bp: Mapped[int | None] = mapped_column(Integer)
    # Pence per hour; ``fixed`` mode only.
    rate: Mapped[int | None] = mapped_column(BigInteger)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid36(), ForeignKey("users.id"))


class TimeEntryOvertime(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """The overtime portion of one time entry (at most one row per entry)."""

    __tablename__ = "time_entry_overtime"
    __table_args__ = (
        UniqueConstraint("time_entry_id", name="uq_time_entry_overtime_time_entry_id"),
        ForeignKeyConstraint(
            ["organization_id", "time_entry_id"],
            ["time_entries.organization_id", "time_entries.id"],
            name="fk_time_entry_overtime_time_entries",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_time_entry_overtime_organization_members",
        ),
        CheckConstraint("overtime_minutes > 0", name="overtime_minutes"),
        CheckConstraint("rate_source IN ('staff', 'grade', 'none')", name="rate_source"),
        CheckConstraint("overtime_rate >= 0", name="overtime_rate"),
        CheckConstraint("overtime_cost >= 0", name="overtime_cost"),
        CheckConstraint("premium_cost >= 0", name="premium_cost"),
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="status"),
        Index("ix_time_entry_overtime_organization_id_user_id", "organization_id", "user_id"),
        Index("ix_time_entry_overtime_organization_id_status", "organization_id", "status"),
    )

    time_entry_id: Mapped[uuid.UUID] = mapped_column(Uuid36(), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid36(), nullable=False)
    overtime_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    # On a job of a client other than the built-in Internal client; false: unallocated.
    allocated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    overtime_rate_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid36(), ForeignKey("overtime_rates.id", ondelete="SET NULL")
    )
    rate_source: Mapped[str] = mapped_column(String(32), nullable=False)
    # Pence per hour actually applied (0 when no overtime rate applies).
    overtime_rate: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # overtime_minutes at overtime_rate; premium_cost = that less the standard cost of the same
    # minutes (never below 0).
    overtime_cost: Mapped[int] = mapped_column(BigInteger, nullable=False)
    premium_cost: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid36(), ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid36(), ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    decision_note: Mapped[str | None] = mapped_column(Text)


__all__ = [
    "HOURS_BASES",
    "OVERTIME_LEVELS",
    "OVERTIME_MODES",
    "OVERTIME_SOURCES",
    "OVERTIME_STATUSES",
    "OvertimeRate",
    "StaffHoursBasis",
    "TimeEntryOvertime",
]
