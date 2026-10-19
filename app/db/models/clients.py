"""Clients of the practice, their contacts and the services they take (client-specific rates
are ``rate_rules``, see ``app/db/models/rates.py``)."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, SoftDeleteMixin, TimestampMixin

CLIENT_TYPES = (
    "individual",
    "sole_trader",
    "partnership",
    "llp",
    "limited_company",
    "trust",
    "charity",
    "other",
)
CLIENT_STATUSES = ("prospect", "active", "inactive", "archived")
# How a client relates to the central client service: ``local`` has no link, the others are
# linked and say how fresh the centrally managed name and address are.
CENTRAL_SYNC_STATES = ("local", "synced", "stale", "failed")


def _member_fk(column: str, table: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["organization_id", column],
        ["organization_members.organization_id", "organization_members.user_id"],
        name=f"fk_{table}_organization_id_{column}_organization_members",
    )


class Client(Base, IdMixin, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "clients"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_clients_organization_id_id"),
        _member_fk("partner_user_id", "clients"),
        _member_fk("manager_user_id", "clients"),
        ForeignKeyConstraint(
            ["organization_id", "default_vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_clients_organization_id_default_vat_rate_id_vat_rates",
        ),
        ForeignKeyConstraint(
            ["organization_id", "default_service_id"],
            ["services.organization_id", "services.id"],
            name="fk_clients_organization_id_default_service_id_services",
        ),
        ForeignKeyConstraint(
            ["organization_id", "invoice_template_id"],
            ["invoice_templates.organization_id", "invoice_templates.id"],
            name="fk_clients_invoice_template_invoice_templates",
        ),
        ForeignKeyConstraint(
            ["organization_id", "merged_into_client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_clients_organization_id_merged_into_client_id_clients",
        ),
        CheckConstraint(
            "central_sync_state IN ('local', 'synced', 'stale', 'failed')",
            name="central_sync_state",
        ),
        CheckConstraint(
            "central_client_id IS NULL OR length(central_client_id) BETWEEN 1 AND 64",
            name="central_client_id_length",
        ),
        CheckConstraint(
            "(central_client_id IS NULL) = (central_sync_state = 'local')",
            name="central_state_matches_link",
        ),
        CheckConstraint(
            "merged_into_client_id IS NULL OR merged_into_client_id <> id",
            name="not_merged_into_itself",
        ),
        CheckConstraint(
            "invoice_email IS NULL OR length(invoice_email) BETWEEN 3 AND 254",
            name="invoice_email_length",
        ),
        CheckConstraint(
            "client_type IN ('individual', 'sole_trader', 'partnership', 'llp', "
            "'limited_company', 'trust', 'charity', 'other')",
            name="client_type",
        ),
        CheckConstraint("status IN ('prospect', 'active', 'inactive', 'archived')", name="status"),
        CheckConstraint("length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("code ~ '^[A-Z0-9_-]{1,20}$'", name="code_format"),
        CheckConstraint(
            "(year_end_month IS NULL) = (year_end_day IS NULL)", name="year_end_complete"
        ),
        CheckConstraint("year_end_month BETWEEN 1 AND 12", name="year_end_month"),
        CheckConstraint("year_end_day BETWEEN 1 AND 31", name="year_end_day"),
        CheckConstraint(
            "payment_terms_days IS NULL OR payment_terms_days BETWEEN 0 AND 365",
            name="payment_terms_days",
        ),
        Index(
            "uq_clients_organization_id_code",
            "organization_id",
            "code",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        CheckConstraint(
            "aml_status IN ('not_started', 'pending', 'clear', 'review', 'failed')",
            name="aml_status",
        ),
        CheckConstraint("health_score BETWEEN 0 AND 100", name="health_score"),
        CheckConstraint("segment IS NULL OR length(segment) BETWEEN 1 AND 40", name="segment"),
        CheckConstraint(
            "accounting_software IS NULL OR accounting_software IN "
            "('xero', 'quickbooks', 'sage', 'freeagent', 'other')",
            name="accounting_software",
        ),
        Index("ix_clients_organization_id_name_lower", "organization_id", text("lower(name)")),
        Index("ix_clients_organization_id_manager_user_id", "organization_id", "manager_user_id"),
        # A central client is enabled at most once per practice.
        Index(
            "uq_clients_organization_id_central_client_id",
            "organization_id",
            "central_client_id",
            unique=True,
            postgresql_where=text("central_client_id IS NOT NULL AND deleted_at IS NULL"),
        ),
        Index(
            "ix_clients_organization_id_merged_into_client_id",
            "organization_id",
            "merged_into_client_id",
            postgresql_where=text("merged_into_client_id IS NOT NULL"),
        ),
    )

    # A short reference such as "ACME01"; generated from the sequence when not given.
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    client_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="limited_company")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    address_line1: Mapped[str | None] = mapped_column(Text)
    address_line2: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    postcode: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str] = mapped_column(Text, nullable=False, server_default="GB")
    # Companies House number, HMRC unique taxpayer reference and VAT registration number.
    company_number: Mapped[str | None] = mapped_column(Text)
    utr: Mapped[str | None] = mapped_column(Text)
    vat_number: Mapped[str | None] = mapped_column(Text)
    year_end_month: Mapped[int | None] = mapped_column(SmallInteger)
    year_end_day: Mapped[int | None] = mapped_column(SmallInteger)
    partner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Overrides the practice's payment terms on this client's invoices.
    payment_terms_days: Mapped[int | None] = mapped_column(SmallInteger)
    default_vat_rate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    billing_notes: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    # Practice-defined tier shown on the client list (Bronze, Silver, Gold, Platinum, ...).
    segment: Mapped[str | None] = mapped_column(Text)
    # Anti-money-laundering check state (UK MLR 2017 customer due diligence).
    aml_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="not_started")
    aml_checked_on: Mapped[date | None] = mapped_column(Date)
    # 0-100; set by the practice (later computed from deadlines, debt and recoverability).
    health_score: Mapped[int | None] = mapped_column(SmallInteger)
    # Bookkeeping software the client uses (shown as a badge, e.g. Xero).
    accounting_software: Mapped[str | None] = mapped_column(Text)
    # Where invoices are emailed; it comes before the billing and primary contacts.
    invoice_email: Mapped[str | None] = mapped_column(Text)
    # The service new jobs and invoices of the client suggest.
    default_service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    invoice_template_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # The central client this one is linked to. While linked, the name and the address are
    # managed centrally (``central_synced_at`` is when they were last confirmed against it).
    central_client_id: Mapped[str | None] = mapped_column(Text)
    central_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    central_sync_state: Mapped[str] = mapped_column(Text, nullable=False, server_default="local")
    # Set on a client that was merged into another: everything it had now belongs there.
    merged_into_client_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class ClientContact(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "client_contacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_client_contacts_organization_id_client_id_clients",
            ondelete="CASCADE",
        ),
        CheckConstraint("length(name) BETWEEN 1 AND 120", name="name_length"),
        Index("ix_client_contacts_organization_id_client_id", "organization_id", "client_id"),
        # One primary contact per client.
        Index(
            "uq_client_contacts_client_id_primary",
            "client_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Invoices are addressed and emailed to billing contacts.
    is_billing: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class ClientService(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A service the client takes from the practice (the engagement), with its agreed fee
    and how often it recurs. Drives the Services column and recurring jobs and billing."""

    __tablename__ = "client_services"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_client_services_organization_id_client_id_clients",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_client_services_organization_id_service_id_services",
        ),
        CheckConstraint(
            "frequency IN ('one_off', 'monthly', 'quarterly', 'annually')", name="frequency"
        ),
        CheckConstraint("fee IS NULL OR fee >= 0", name="fee"),
        CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR start_date <= end_date", name="date_order"
        ),
        UniqueConstraint("client_id", "service_id", name="uq_client_services_client_id_service_id"),
        Index("ix_client_services_organization_id_service_id", "organization_id", "service_id"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Agreed fee per occurrence; null uses the service's default fee.
    fee: Mapped[int | None] = mapped_column(BigInteger)
    frequency: Mapped[str] = mapped_column(Text, nullable=False, server_default="annually")
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


__all__ = ["Client", "ClientContact", "ClientService"]
