"""Tenancy: the practice (an organization), its roles and its staff (members)."""

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal

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
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, IdMixin, SoftDeleteMixin, TimestampMixin
from app.db.models.user import User

if TYPE_CHECKING:
    from app.db.models.practice import Grade

# ``suspended`` and ``removed`` are the original (V1) values; ``inactive`` and ``left`` are what
# the staff API writes. A V1 value behaves like its successor and is reported as it.
MEMBER_STATUSES = ("active", "suspended", "removed", "inactive", "left")
# Staff who no longer work for the practice: they cannot sign in and are not offered for new work.
GONE_STATUSES = ("removed", "left")
# How a fixed fee counts as WIP, and which rates price time (see the columns below).
FixedFeeWipMode = Literal["on_completion", "by_budget_progress"]
RateMode = Literal["staff_first", "grade_only"]
# How a practice locks: a lock covers one week, month, quarter or accounting year.
LockGranularity = Literal["weekly", "monthly", "quarterly", "annually"]


class Organization(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    """An accountancy practice. Every business row carries its ``organization_id``.

    Money is stored everywhere as integer minor units (pence) in ``currency``.
    """

    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$'", name="slug_format"),
        CheckConstraint("week_start BETWEEN 1 AND 7", name="week_start"),
        CheckConstraint("minutes_per_day BETWEEN 1 AND 1440", name="minutes_per_day"),
        CheckConstraint("length(name) BETWEEN 1 AND 120", name="name_length"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint("payment_terms_days BETWEEN 0 AND 365", name="payment_terms_days"),
        CheckConstraint("default_charge_rate >= 0", name="default_charge_rate"),
        CheckConstraint("time_increment_minutes IN (1, 5, 6, 10, 15, 30, 60)", name="increment"),
        CheckConstraint(
            "fixed_fee_wip_mode IN ('on_completion', 'by_budget_progress')",
            name="fixed_fee_wip_mode",
        ),
        CheckConstraint(
            "wip_adjustment_approval_threshold >= 0", name="wip_adjustment_approval_threshold"
        ),
        CheckConstraint(
            "wip_write_off_approval_threshold_percent IS NULL OR "
            "wip_write_off_approval_threshold_percent BETWEEN 0 AND 10000",
            name="wip_write_off_approval_threshold_percent",
        ),
        CheckConstraint("receipt_required_threshold >= 0", name="receipt_required_threshold"),
        CheckConstraint("invoice_approval_threshold >= 0", name="invoice_approval_threshold"),
        CheckConstraint(
            "payment_reminder_days_after_due BETWEEN 1 AND 365",
            name="payment_reminder_days_after_due",
        ),
        CheckConstraint(
            "cardinality(payment_reminder_schedule) <= 12", name="payment_reminder_schedule"
        ),
        CheckConstraint("mileage_rate >= 0", name="mileage_rate"),
        CheckConstraint("expense_markup_bp BETWEEN 0 AND 100000", name="expense_markup_bp"),
        CheckConstraint("rate_mode IN ('staff_first', 'grade_only')", name="rate_mode"),
        CheckConstraint(
            "overtime_multiplier_bp BETWEEN 10000 AND 100000", name="overtime_multiplier_bp"
        ),
        CheckConstraint(
            "period_lock_granularity IN ('weekly', 'monthly', 'quarterly', 'annually')",
            name="period_lock_granularity",
        ),
        CheckConstraint(
            "financial_year_start_month BETWEEN 1 AND 12 AND "
            "financial_year_start_day BETWEEN 1 AND 28",
            name="financial_year_start",
        ),
        Index(
            "uq_organizations_slug",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default="Europe/London")
    week_start: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    working_days: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), nullable=False, server_default=text("'{1,2,3,4,5}'")
    )
    minutes_per_day: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("450")
    )
    # Billing settings
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default="GBP")
    vat_registered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    vat_number: Mapped[str | None] = mapped_column(Text)
    company_number: Mapped[str | None] = mapped_column(Text)
    payment_terms_days: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("30")
    )
    # Hourly rate used when neither the job, the client nor the staff member sets one.
    default_charge_rate: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # Time is rounded up to this many minutes when it is logged (6 = tenths of an hour).
    time_increment_minutes: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("6")
    )
    # Timesheets must be approved before their time can be billed.
    require_timesheet_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # Time of people who hold ``timesheet.exempt`` (Owner, Admin and Partner by default) counts
    # as approved without a timesheet decision.
    auto_approve_privileged_time: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # How the unbilled part of a fixed fee counts as work in progress: once the job is completed
    # (``on_completion``) or in step with the budget hours used (``by_budget_progress``).
    fixed_fee_wip_mode: Mapped[FixedFeeWipMode] = mapped_column(
        Text, nullable=False, server_default="on_completion"
    )
    # A WIP write-on or write-off above this many pence needs approval (0: every adjustment does).
    wip_adjustment_approval_threshold: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("50000")
    )
    # A write-off above this percentage (basis points; 1000 = 10.00%) of the job's or client's
    # current WIP also needs approval, in addition to the flat-pence threshold above. Null turns
    # the percentage rule off, leaving only the flat-pence threshold.
    wip_write_off_approval_threshold_percent: Mapped[int | None] = mapped_column(
        Integer, nullable=True, server_default=text("1000")
    )
    # An expense above this many pence (gross) must carry a receipt.
    receipt_required_threshold: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("2500")
    )
    # When on, an invoice above ``invoice_approval_threshold`` pence (gross; 0: every invoice)
    # must be approved before it can be issued.
    invoice_approval_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    invoice_approval_threshold: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # Overdue invoices are reminded about automatically (to the client's billing recipients) once
    # they are ``payment_reminder_days_after_due`` days past their due date.
    payment_reminders_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    payment_reminder_days_after_due: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("7")
    )
    # The days, from the due date, on which an unpaid invoice is reminded about (negative: before).
    # Empty: the single reminder ``payment_reminder_days_after_due`` days after the due date.
    payment_reminder_schedule: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), nullable=False, server_default=text("'{}'")
    )
    # Pence per mile claimed, and the markup in basis points added when an expense is recharged.
    mileage_rate: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("45"))
    expense_markup_bp: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # Which rates price time: ``staff_first`` takes a staff member's own rate before their grade's;
    # ``grade_only`` skips the staff-level rate (rung 6 of the ladder) so the grade rate applies.
    rate_mode: Mapped[RateMode] = mapped_column(Text, nullable=False, server_default="staff_first")
    # Period locks: locks are exactly one period of this length inside the accounting year that
    # starts on ``financial_year_start_month``/``_day`` (1 January: the calendar year). It stays
    # unconfirmed (``period_lock_configured`` false) until someone makes the choice.
    period_lock_granularity: Mapped[LockGranularity] = mapped_column(
        Text, nullable=False, server_default="weekly"
    )
    financial_year_start_month: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )
    financial_year_start_day: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )
    period_lock_configured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Overtime: when on, time marked as overtime is priced at its normal rates times the multiplier
    # (basis points, 15000 = 1.5x) unless the staff member has their own.
    overtime_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    overtime_multiplier_bp: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("15000")
    )
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    address_line1: Mapped[str | None] = mapped_column(Text)
    address_line2: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    postcode: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str] = mapped_column(Text, nullable=False, server_default="GB")
    bank_details: Mapped[str | None] = mapped_column(Text)
    invoice_footer: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )


class Permission(Base, IdMixin, TimestampMixin):
    """Global catalogue, seeded by migration from ``app.core.permissions``."""

    __tablename__ = "permissions"
    __table_args__ = (UniqueConstraint("code", name="uq_permissions_code"),)

    code: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)


class Role(Base, IdMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_roles_organization_id_id"),
        CheckConstraint("length(name) BETWEEN 1 AND 60", name="name_length"),
        Index(
            "uq_roles_organization_id_name_lower",
            "organization_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "uq_roles_organization_id_system_key",
            "organization_id",
            "system_key",
            unique=True,
            postgresql_where=text("system_key IS NOT NULL"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    system_key: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)

    permissions: Mapped[list[Permission]] = relationship(
        secondary="role_permissions", lazy="raise", order_by="Permission.code"
    )

    @property
    def is_system(self) -> bool:
        return self.system_key is not None


class RolePermission(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (PrimaryKeyConstraint("role_id", "permission_id"),)

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MemberPermissionOverride(Base, IdMixin):
    """A permission granted to (``grant``) or taken from (``deny``) one member, on top of their
    role. The member's permissions are the role's, plus the grants, less the denials."""

    __tablename__ = "member_permission_overrides"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "user_id",
            "permission_code",
            name="uq_member_permission_overrides_member_code",
        ),
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_member_permission_overrides_organization_members",
            ondelete="CASCADE",
        ),
        CheckConstraint("effect IN ('grant', 'deny')", name="effect"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    permission_code: Mapped[str] = mapped_column(
        Text, ForeignKey("permissions.code", ondelete="CASCADE"), nullable=False
    )
    effect: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OrganizationMember(Base, IdMixin, TimestampMixin):
    """A staff member of the practice.

    Never hard-deleted while anything refers to them: a removed member keeps the row with
    ``status = 'removed'`` (or ``left``) so the time, rates and invoices that name them stay
    intact. Someone without a login is a member whose ``users`` row is an inactive placeholder
    (``login_enabled`` is false): every table keyed by user keeps working for them.
    """

    __tablename__ = "organization_members"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "user_id", name="uq_organization_members_organization_id_user_id"
        ),
        ForeignKeyConstraint(
            ["organization_id", "role_id"],
            ["roles.organization_id", "roles.id"],
            name="fk_organization_members_organization_id_role_id_roles",
        ),
        ForeignKeyConstraint(
            ["organization_id", "manager_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_organization_members_manager_organization_members",
        ),
        ForeignKeyConstraint(
            ["organization_id", "grade_id"],
            ["grades.organization_id", "grades.id"],
            name="fk_organization_members_organization_id_grade_id_grades",
        ),
        CheckConstraint(
            "status IN ('active', 'suspended', 'removed', 'inactive', 'left')", name="status"
        ),
        CheckConstraint("manager_user_id <> user_id", name="not_own_manager"),
        CheckConstraint(
            "overtime_multiplier_bp IS NULL OR overtime_multiplier_bp BETWEEN 10000 AND 100000",
            name="overtime_multiplier_bp",
        ),
        CheckConstraint("weekly_capacity_minutes BETWEEN 0 AND 10080", name="weekly_capacity"),
        CheckConstraint("working_minutes_per_day BETWEEN 1 AND 1440", name="working_minutes"),
        CheckConstraint(
            "cardinality(working_days) BETWEEN 1 AND 7 "
            "AND working_days <@ ARRAY[1, 2, 3, 4, 5, 6, 7]::smallint[]",
            name="working_days",
        ),
        CheckConstraint(
            "leaving_date IS NULL OR start_date IS NULL OR leaving_date >= start_date",
            name="leaving_after_start",
        ),
        CheckConstraint("staff_code ~ '^[A-Z0-9_-]{1,20}$'", name="staff_code_format"),
        CheckConstraint(
            "work_email IS NULL OR length(work_email) BETWEEN 3 AND 254", name="work_email_length"
        ),
        CheckConstraint(
            "mobile_number IS NULL OR mobile_number ~ '^[+]?[0-9]{7,15}$'",
            name="mobile_number_format",
        ),
        Index("ix_organization_members_user_id", "user_id"),
        Index(
            "ix_organization_members_organization_id_manager_user_id",
            "organization_id",
            "manager_user_id",
        ),
        Index("ix_organization_members_organization_id_grade_id", "organization_id", "grade_id"),
        Index(
            "uq_organization_members_organization_id_initials",
            "organization_id",
            text("upper(initials)"),
            unique=True,
            postgresql_where=text("initials IS NOT NULL AND status <> 'removed'"),
        ),
        Index(
            "uq_organization_members_organization_id_staff_code",
            "organization_id",
            text("upper(staff_code)"),
            unique=True,
        ),
        # No two active staff share a work email.
        Index(
            "uq_organization_members_organization_id_work_email_active",
            "organization_id",
            text("lower(work_email)"),
            unique=True,
            postgresql_where=text("work_email IS NOT NULL AND status = 'active'"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    role_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    # The person who approves this member's timesheets.
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    weekly_capacity_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("2250")
    )
    # Target share of capacity that should be chargeable, in basis points (7500 = 75%).
    chargeable_target_bp: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("7500")
    )
    # The person's own overtime multiplier (basis points); null: the practice's.
    overtime_multiplier_bp: Mapped[int | None] = mapped_column(Integer)
    job_title: Mapped[str | None] = mapped_column(Text)
    initials: Mapped[str | None] = mapped_column(Text)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Staff profile. The code is unique per practice (case-insensitively) and comes from the
    # ``staff`` number sequence when it is not given.
    staff_code: Mapped[str] = mapped_column(Text, nullable=False)
    work_email: Mapped[str | None] = mapped_column(Text)
    grade_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    working_minutes_per_day: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("450")
    )
    working_days: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), nullable=False, server_default=text("'{1,2,3,4,5}'")
    )
    start_date: Mapped[date | None] = mapped_column(Date)
    leaving_date: Mapped[date | None] = mapped_column(Date)
    # False for staff without an account: ``users.is_active`` is false and nobody can sign in.
    login_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # When the sign-in invitation was last sent. A login stays "invited" until the person has
    # chosen a password (``users.password_changed_at`` moves past this moment).
    invited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Where SMS notifications go: digits with an optional leading +. Set by the person in their
    # notification settings; without it no SMS is sent.
    mobile_number: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(lazy="raise", foreign_keys=[user_id])
    role: Mapped[Role] = relationship(
        lazy="raise",
        primaryjoin="OrganizationMember.role_id == Role.id",
        foreign_keys=[role_id],
        viewonly=True,
    )
    # Permissions given to, or taken from, this person on top of their role.
    permission_overrides: Mapped[list[MemberPermissionOverride]] = relationship(
        "MemberPermissionOverride",
        lazy="raise",
        primaryjoin=(
            "and_(OrganizationMember.organization_id == MemberPermissionOverride.organization_id, "
            "OrganizationMember.user_id == MemberPermissionOverride.user_id)"
        ),
        foreign_keys="[MemberPermissionOverride.organization_id, MemberPermissionOverride.user_id]",
        viewonly=True,
        order_by="MemberPermissionOverride.permission_code",
    )
    grade: Mapped["Grade | None"] = relationship(
        "Grade",
        lazy="raise",
        primaryjoin="OrganizationMember.grade_id == Grade.id",
        foreign_keys=[grade_id],
        viewonly=True,
    )


__all__ = [
    "GONE_STATUSES",
    "MEMBER_STATUSES",
    "Organization",
    "OrganizationMember",
    "Permission",
    "Role",
    "RolePermission",
]
