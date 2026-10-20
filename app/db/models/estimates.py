"""Estimates (quotes sent to clients) and recurring invoice schedules.

An estimate is priced when it is written: its lines snapshot the VAT rate, as invoice lines do.
Converting it copies those lines into a draft invoice; nothing moves WIP. A recurring schedule
holds a template of lines that is priced again every time it runs, so a VAT change applies to
the next invoice. Each run is a row with a unique (schedule, run date): the daily job can be
delivered twice and still raises one invoice.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin

ESTIMATE_STATUSES = ("draft", "sent", "accepted", "declined")
RECURRING_FREQUENCIES = ("weekly", "monthly", "quarterly", "annually")
RECURRING_STATUSES = ("active", "paused", "ended")
RUN_STATUSES = ("succeeded", "failed")


def _client_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", "client_id"],
        ["clients.organization_id", "clients.id"],
        name=f"fk_{table}_organization_id_client_id_clients",
    )


def _vat_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", "vat_rate_id"],
        ["vat_rates.organization_id", "vat_rates.id"],
        name=f"fk_{table}_organization_id_vat_rate_id_vat_rates",
    )


def _job_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", "job_id"],
        ["jobs.organization_id", "jobs.id"],
        name=f"fk_{table}_organization_id_job_id_jobs",
    )


def _service_fk(table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", "service_id"],
        ["services.organization_id", "services.id"],
        name=f"fk_{table}_organization_id_service_id_services",
    )


class Estimate(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "estimates"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_estimates_organization_id_id"),
        _client_fk("estimates"),
        ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_estimates_organization_id_client_id_job_id_jobs",
        ),
        ForeignKeyConstraint(
            ["organization_id", "converted_invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name="fk_estimates_organization_id_converted_invoice_id_invoices",
        ),
        CheckConstraint("status IN ('draft', 'sent', 'accepted', 'declined')", name="status"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint("(status = 'draft') = (number IS NULL)", name="number_when_sent"),
        CheckConstraint(
            "expiry_date IS NULL OR issue_date IS NULL OR expiry_date >= issue_date",
            name="expiry_after_issue",
        ),
        CheckConstraint("gross_total = net_total + vat_total", name="gross_total"),
        Index(
            "uq_estimates_organization_id_number",
            "organization_id",
            "number",
            unique=True,
            postgresql_where=text("number IS NOT NULL"),
        ),
        Index("ix_estimates_organization_id_client_id", "organization_id", "client_id"),
        Index("ix_estimates_organization_id_status", "organization_id", "status"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Assigned from the "estimate" sequence when sent; drafts have none.
    number: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    issue_date: Mapped[date | None] = mapped_column(Date)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    reference: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    net_total: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    vat_total: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    gross_total: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    converted_invoice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class EstimateLine(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "estimate_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "estimate_id"],
            ["estimates.organization_id", "estimates.id"],
            name="fk_estimate_lines_organization_id_estimate_id_estimates",
            ondelete="CASCADE",
        ),
        _job_fk("estimate_lines"),
        _service_fk("estimate_lines"),
        _vat_fk("estimate_lines"),
        CheckConstraint("quantity > 0", name="quantity"),
        CheckConstraint("vat_rate_bp BETWEEN 0 AND 10000", name="vat_rate_bp"),
        CheckConstraint("length(description) BETWEEN 1 AND 1000", name="description_length"),
        Index("ix_estimate_lines_estimate_id_position", "estimate_id", "position"),
    )

    estimate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default=text("1")
    )
    unit_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vat_rate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    vat_rate_bp: Mapped[int] = mapped_column(Integer, nullable=False)
    vat_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)


class RecurringSchedule(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "recurring_schedules"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_recurring_schedules_organization_id_id"),
        _client_fk("recurring_schedules"),
        CheckConstraint(
            "frequency IN ('weekly', 'monthly', 'quarterly', 'annually')", name="frequency"
        ),
        CheckConstraint("status IN ('active', 'paused', 'ended')", name="status"),
        CheckConstraint("length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="end_after_start"),
        CheckConstraint("next_run_date >= start_date", name="next_after_start"),
        Index("ix_recurring_schedules_organization_id_client_id", "organization_id", "client_id"),
        Index(
            "ix_recurring_schedules_due",
            "next_run_date",
            postgresql_where=text("status = 'active'"),
        ),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    frequency: Mapped[str] = mapped_column(Text, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    next_run_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    reference: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    last_run_on: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class RecurringLine(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A template line: priced again (VAT resolved) on every run."""

    __tablename__ = "recurring_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["recurring_schedules.organization_id", "recurring_schedules.id"],
            name="fk_recurring_lines_schedule",
            ondelete="CASCADE",
        ),
        _job_fk("recurring_lines"),
        _service_fk("recurring_lines"),
        _vat_fk("recurring_lines"),
        CheckConstraint("quantity > 0", name="quantity"),
        CheckConstraint("length(description) BETWEEN 1 AND 1000", name="description_length"),
        Index("ix_recurring_lines_schedule_id_position", "schedule_id", "position"),
    )

    schedule_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default=text("1")
    )
    unit_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The chosen rate; null means the usual resolution (service, client, practice default).
    vat_rate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class RecurringRun(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "recurring_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["recurring_schedules.organization_id", "recurring_schedules.id"],
            name="fk_recurring_runs_schedule",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name="fk_recurring_runs_organization_id_invoice_id_invoices",
        ),
        UniqueConstraint("schedule_id", "run_date", name="uq_recurring_runs_schedule_id_run_date"),
        CheckConstraint("status IN ('succeeded', 'failed')", name="status"),
        CheckConstraint(
            "(status = 'succeeded') = (invoice_id IS NOT NULL)", name="invoice_when_succeeded"
        ),
        Index("ix_recurring_runs_schedule_id_run_date", "schedule_id", "run_date"),
    )

    schedule_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    ran_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


__all__ = [
    "ESTIMATE_STATUSES",
    "RECURRING_FREQUENCIES",
    "RECURRING_STATUSES",
    "RUN_STATUSES",
    "Estimate",
    "EstimateLine",
    "RecurringLine",
    "RecurringRun",
    "RecurringSchedule",
]
