"""Practice set-up: VAT rates, services, activity codes, grades, segments, branches and number
sequences.

Conventions shared by every business table:

* ``organization_id`` on every row; references inside a practice use composite foreign
  keys ``(organization_id, x_id)``, so a row can never point into another practice.
* Money is ``BIGINT`` minor units (pence). Rates are minor units per hour.
* VAT rates are basis points: 2000 = 20.00%.
* Durations are whole minutes.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin
from app.db.types import NOW, UTCDateTime, Uuid36

VAT_CODES = ("standard", "reduced", "zero", "exempt", "outside_scope")
BILLING_BASES = ("hourly", "fixed")
SEQUENCE_KINDS = ("client", "job", "invoice", "credit_note", "staff", "ticket", "estimate")


class VatRate(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "vat_rates"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_vat_rates_organization_id_id"),
        CheckConstraint(
            "code IN ('standard', 'reduced', 'zero', 'exempt', 'outside_scope')", name="code"
        ),
        CheckConstraint("rate_bp BETWEEN 0 AND 10000", name="rate_bp"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 40", name="name_length"),
        Index(
            "uq_vat_rates_organization_id_name_lower",
            text("(CASE WHEN archived_at IS NULL THEN `organization_id` END)"),
            text("(CASE WHEN archived_at IS NULL THEN lower(name) END)"),
            unique=True,
        ),
        # At most one default rate per practice.
        Index(
            "uq_vat_rates_organization_id_default",
            text("(CASE WHEN is_default AND archived_at IS NULL THEN `organization_id` END)"),
            unique=True,
        ),
    )

    name: Mapped[str] = mapped_column(String(40), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    rate_bp: Mapped[int] = mapped_column(Integer, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Service(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A kind of work the practice sells: year-end accounts, VAT returns, payroll, ..."""

    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_services_organization_id_id"),
        ForeignKeyConstraint(
            ["organization_id", "vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_services_organization_id_vat_rate_id_vat_rates",
        ),
        CheckConstraint("billing_basis IN ('hourly', 'fixed')", name="billing_basis"),
        CheckConstraint("default_fee IS NULL OR default_fee >= 0", name="default_fee"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 120", name="name_length"),
        CheckConstraint("REGEXP_LIKE(code, '^[A-Z0-9_-]{1,12}$', 'c')", name="code_format"),
        CheckConstraint(
            "invoice_description IS NULL OR char_length(invoice_description) BETWEEN 1 AND 500",
            name="invoice_description_length",
        ),
        Index("uq_services_organization_id_code", "organization_id", "code", unique=True),
        Index(
            "uq_services_organization_id_name_lower",
            "organization_id",
            text("(lower(name))"),
            unique=True,
        ),
    )

    code: Mapped[str] = mapped_column(String(12), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    billing_basis: Mapped[str] = mapped_column(String(32), nullable=False, server_default="hourly")
    # Suggested fixed fee for new jobs of this service.
    default_fee: Mapped[int | None] = mapped_column(BigInteger)
    # Whether work on this service is billable by default: jobs created without a billing
    # basis start as ``non_billable`` when it is false.
    default_billable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    vat_rate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid36())
    # Wording suggested for invoice lines of this service.
    invoice_description: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class ActivityCode(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """What a piece of time was spent on. Non-chargeable codes (admin, training, holiday)
    are how internal time is recorded and how utilisation is measured."""

    __tablename__ = "activity_codes"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_activity_codes_organization_id_id"),
        ForeignKeyConstraint(
            ["organization_id", "vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_activity_codes_organization_id_vat_rate_id_vat_rates",
        ),
        CheckConstraint("REGEXP_LIKE(code, '^[A-Z0-9_-]{1,12}$', 'c')", name="code_format"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 120", name="name_length"),
        CheckConstraint(
            "invoice_description IS NULL OR char_length(invoice_description) BETWEEN 1 AND 500",
            name="invoice_description_length",
        ),
        Index("uq_activity_codes_organization_id_code", "organization_id", "code", unique=True),
        Index(
            "uq_activity_codes_organization_id_name_lower",
            "organization_id",
            text("(lower(name))"),
            unique=True,
        ),
    )

    code: Mapped[str] = mapped_column(String(12), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # The default billable status of time recorded against the code.
    is_chargeable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    vat_rate_id: Mapped[uuid.UUID | None] = mapped_column(Uuid36())
    invoice_description: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class ServiceTaskTemplate(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """One default task of a service: added to a new job of the service when the person asks.

    ``due_offset_days`` is counted from the job's start date. A service has at most 100.
    """

    __tablename__ = "service_task_templates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_service_task_templates_service_id_services",
            ondelete="CASCADE",
        ),
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="title_length"),
        CheckConstraint(
            "description IS NULL OR char_length(description) <= 2000", name="description_length"
        ),
        CheckConstraint("budget_minutes IS NULL OR budget_minutes >= 0", name="budget_minutes"),
        CheckConstraint(
            "due_offset_days IS NULL OR due_offset_days BETWEEN 0 AND 3650",
            name="due_offset_days",
        ),
        Index(
            "ix_service_task_templates_organization_id_service_id_sort_order",
            "organization_id",
            "service_id",
            "sort_order",
        ),
    )

    service_id: Mapped[uuid.UUID] = mapped_column(Uuid36(), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    budget_minutes: Mapped[int | None] = mapped_column(Integer)
    due_offset_days: Mapped[int | None] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class ServiceActivityCode(Base, OrgScopedMixin):
    """An activity type allowed on a service. A service without links allows every type."""

    __tablename__ = "service_activity_codes"
    __table_args__ = (
        PrimaryKeyConstraint("service_id", "activity_code_id"),
        ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_service_activity_codes_service_id_services",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "activity_code_id"],
            ["activity_codes.organization_id", "activity_codes.id"],
            name="fk_service_activity_codes_activity_code_id_activity_codes",
            ondelete="CASCADE",
        ),
        Index(
            "ix_service_activity_codes_organization_id_activity_code_id",
            "organization_id",
            "activity_code_id",
        ),
    )

    service_id: Mapped[uuid.UUID] = mapped_column(Uuid36(), nullable=False)
    activity_code_id: Mapped[uuid.UUID] = mapped_column(Uuid36(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, server_default=NOW)


class Grade(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A staff grade (Partner, Manager, Senior, ...) with default rates and a chargeable target.

    The default rates are the baseline of the grade's rate level: a dated rate rule for the
    grade (``rate_rules``) overrides them for the days it is in force.
    """

    __tablename__ = "grades"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_grades_organization_id_id"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 80", name="name_length"),
        CheckConstraint("REGEXP_LIKE(code, '^[A-Z0-9_-]{1,12}$', 'c')", name="code_format"),
        CheckConstraint(
            "description IS NULL OR char_length(description) BETWEEN 1 AND 500",
            name="description_length",
        ),
        CheckConstraint(
            "default_cost_rate IS NULL OR default_cost_rate >= 0", name="default_cost_rate"
        ),
        CheckConstraint(
            "default_charge_rate IS NULL OR default_charge_rate >= 0", name="default_charge_rate"
        ),
        CheckConstraint(
            "default_chargeable_target_bp IS NULL "
            "OR default_chargeable_target_bp BETWEEN 0 AND 10000",
            name="default_chargeable_target_bp",
        ),
        Index("uq_grades_organization_id_code", "organization_id", "code", unique=True),
        Index(
            "uq_grades_organization_id_name_lower",
            "organization_id",
            text("(lower(name))"),
            unique=True,
        ),
    )

    code: Mapped[str] = mapped_column(String(12), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    # Pence per hour; null = the grade sets no rate of that kind.
    default_cost_rate: Mapped[int | None] = mapped_column(BigInteger)
    default_charge_rate: Mapped[int | None] = mapped_column(BigInteger)
    # Basis points (7500 = 75%); the starting target of staff given this grade.
    default_chargeable_target_bp: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class DocumentSequence(Base, OrgScopedMixin, TimestampMixin):
    """Gapless numbering per practice. The row is locked ``FOR UPDATE`` while a number is
    taken, inside the transaction that uses it, so a rolled-back invoice frees its number."""

    __tablename__ = "document_sequences"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "kind"),
        CheckConstraint(
            "kind IN ('client', 'job', 'invoice', 'credit_note', 'staff', 'ticket', 'estimate')",
            name="kind",
        ),
        CheckConstraint("next_number >= 1", name="next_number"),
        CheckConstraint("padding BETWEEN 1 AND 10", name="padding"),
        CheckConstraint("REGEXP_LIKE(prefix, '^[A-Za-z0-9/_-]{0,12}$', 'c')", name="prefix_format"),
        CheckConstraint("REGEXP_LIKE(suffix, '^[A-Za-z0-9/_-]{0,12}$', 'c')", name="suffix_format"),
    )

    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    prefix: Mapped[str] = mapped_column(String(12), nullable=False, server_default="")
    # Printed after the number of an invoice, credit note or estimate; empty elsewhere.
    suffix: Mapped[str] = mapped_column(String(12), nullable=False, server_default="")
    next_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    padding: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("5"))


__all__ = [
    "ActivityCode",
    "DocumentSequence",
    "Grade",
    "Service",
    "ServiceActivityCode",
    "VatRate",
]


class Segment(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A client segment (Gold, Retail, ...) the practice defines in its settings.

    Clients keep the segment as text (``clients.segment``), matched to a row here ignoring case:
    a value nobody defined still works, it is just not listed. Renaming a segment renames it on
    its clients.
    """

    __tablename__ = "segments"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_segments_organization_id_id"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 40", name="name_length"),
        CheckConstraint(
            "description IS NULL OR char_length(description) BETWEEN 1 AND 500",
            name="description_length",
        ),
        Index(
            "uq_segments_organization_id_name_lower",
            "organization_id",
            text("(lower(name))"),
            unique=True,
        ),
    )

    name: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class Branch(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A branch (office) of the practice, managed in the settings. A client belongs to at most one
    (``clients.branch_id``); with ``organizations.branches_enabled`` on, every client created or
    edited needs one. A branch that clients have is deactivated, not deleted.
    """

    __tablename__ = "branches"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_branches_organization_id_id"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 40", name="name_length"),
        Index(
            "uq_branches_organization_id_name_lower",
            "organization_id",
            text("(lower(name))"),
            unique=True,
        ),
    )

    name: Mapped[str] = mapped_column(String(40), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class StaffSegment(Base, IdMixin, OrgScopedMixin):
    """A staff member looking after the clients of a segment."""

    __tablename__ = "staff_segments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_staff_segments_organization_id_user_id_members",
        ),
        ForeignKeyConstraint(
            ["organization_id", "segment_id"],
            ["segments.organization_id", "segments.id"],
            name="fk_staff_segments_organization_id_segment_id_segments",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id", "user_id", "segment_id", name="uq_staff_segments_user_id_segment_id"
        ),
        Index("ix_staff_segments_organization_id_segment_id", "organization_id", "segment_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid36(), nullable=False)
    segment_id: Mapped[uuid.UUID] = mapped_column(Uuid36(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=NOW, nullable=False)
