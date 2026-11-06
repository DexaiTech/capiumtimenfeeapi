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

Nothing prices time from these tables yet: time keeps its rates as before.
"""

import uuid
from datetime import date

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin
from app.db.models.clients import _member_fk

HOURS_BASES = ("weekly", "monthly")
OVERTIME_LEVELS = ("grade", "staff")
OVERTIME_MODES = ("multiplier", "fixed")
_NIL = "'00000000-0000-0000-0000-000000000000'::uuid"


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
        # Concurrency-safe: two periods of one person can never cover the same day.
        ExcludeConstraint(
            ("organization_id", "="),
            ("user_id", "="),
            (text("daterange(effective_from, effective_to, '[]')"), "&&"),
            name="ex_staff_hours_bases_user_period",
            using="gist",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    basis: Mapped[str] = mapped_column(Text, nullable=False)
    # Standard working minutes in one week or one month; beyond them is overtime.
    standard_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


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
        # Concurrency-safe: two rates of one grade or one person can never cover the same day.
        ExcludeConstraint(
            ("organization_id", "="),
            (text(f"coalesce(user_id, {_NIL})"), "="),
            (text(f"coalesce(grade_id, {_NIL})"), "="),
            (text("daterange(effective_from, effective_to, '[]')"), "&&"),
            name="ex_overtime_rates_scope_period",
            using="gist",
        ),
    )

    level: Mapped[str] = mapped_column(Text, nullable=False)
    grade_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    # Basis points of the standard cost rate (15000 = 1.5x); ``multiplier`` mode only.
    multiplier_bp: Mapped[int | None] = mapped_column(Integer)
    # Pence per hour; ``fixed`` mode only.
    rate: Mapped[int | None] = mapped_column(BigInteger)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


__all__ = [
    "HOURS_BASES",
    "OVERTIME_LEVELS",
    "OVERTIME_MODES",
    "OvertimeRate",
    "StaffHoursBasis",
]
