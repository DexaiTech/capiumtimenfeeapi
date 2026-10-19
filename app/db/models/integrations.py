"""Integrations: the cache of central clients, the log of every synchronisation attempt and the
cursor of the central change feed.

``integration_sync_logs`` is generic: the central client integration writes ``integration =
'central_client'`` rows and the integrations that follow (central users, practice management,
bookkeeping) write their own into the same table.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin

CENTRAL_CLIENT_STATUSES = ("active", "archived")
SYNC_DIRECTIONS = ("inbound", "outbound")
SYNC_STATUSES = ("ok", "failed", "skipped")
INTEGRATION_KEYS = ("central_client", "central_user", "practice_management", "bookkeeping")
CONNECTION_STATUSES = ("idle", "ok", "error")
SYNC_ITEM_STATUSES = ("pending", "ok", "failed", "skipped")
_INTEGRATION_KEYS_SQL = "'central_client', 'central_user', 'practice_management', 'bookkeeping'"
# An account or tax code of the bookkeeping system: letters, digits and . _ / - (1 to 20).
CODE_PATTERN = "^[A-Za-z0-9][A-Za-z0-9._/-]{0,19}$"


class CentralClientCache(Base, IdMixin, OrgScopedMixin):
    """The last known state of a central client, kept per practice.

    Every live search and every fetch refreshes it, so enabling and syncing keep working
    from here while the central service is unavailable.
    """

    __tablename__ = "central_client_cache"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "central_id",
            name="uq_central_client_cache_organization_id_central_id",
        ),
        CheckConstraint("length(central_id) BETWEEN 1 AND 64", name="central_id_length"),
        CheckConstraint("length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("status IN ('active', 'archived')", name="status"),
        Index(
            "ix_central_client_cache_organization_id_name_lower",
            "organization_id",
            text("lower(name)"),
        ),
        Index(
            "ix_central_client_cache_organization_id_company_number",
            "organization_id",
            "company_number",
        ),
    )

    central_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    company_number: Mapped[str | None] = mapped_column(Text)
    address_line1: Mapped[str | None] = mapped_column(Text)
    address_line2: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    postcode: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str] = mapped_column(Text, nullable=False, server_default="GB")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    # When the central service last changed the record, and when this cache last heard of it.
    source_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class IntegrationSyncLog(Base, IdMixin, OrgScopedMixin):
    """Append-only: one row per synchronisation attempt, successful or not.

    ``external_event_id`` is set only on a row that records an event as processed (``ok`` or
    ``skipped``); the unique index makes a second delivery of the same event a no-op even when
    two arrive at once. Failed attempts and repeated deliveries leave it null (the event id is
    in ``message`` and ``details``), so a retry can still be recorded.
    """

    __tablename__ = "integration_sync_logs"
    __table_args__ = (
        CheckConstraint("integration ~ '^[a-z][a-z0-9_]{1,39}$'", name="integration_format"),
        CheckConstraint("direction IN ('inbound', 'outbound')", name="direction"),
        CheckConstraint("length(operation) BETWEEN 1 AND 60", name="operation_length"),
        CheckConstraint("status IN ('ok', 'failed', 'skipped')", name="status"),
        CheckConstraint("attempt >= 1", name="attempt"),
        CheckConstraint("message IS NULL OR length(message) <= 1000", name="message_length"),
        Index(
            "uq_integration_sync_logs_event",
            "organization_id",
            "integration",
            "external_event_id",
            unique=True,
            postgresql_where=text("external_event_id IS NOT NULL"),
        ),
        Index(
            "ix_integration_sync_logs_organization_id_created_at", "organization_id", "created_at"
        ),
        Index(
            "ix_integration_sync_logs_organization_id_entity_id_created_at",
            "organization_id",
            "entity_id",
            "created_at",
            postgresql_where=text("entity_id IS NOT NULL"),
        ),
    )

    integration: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False, server_default="inbound")
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    external_id: Mapped[str | None] = mapped_column(Text)
    external_event_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class IntegrationCursor(Base, OrgScopedMixin):
    """Where the practice has read an integration's change feed up to."""

    __tablename__ = "integration_cursors"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "integration"),
        CheckConstraint("integration ~ '^[a-z][a-z0-9_]{1,39}$'", name="integration_format"),
    )

    integration: Mapped[str] = mapped_column(Text, nullable=False)
    cursor: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class IntegrationConnection(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A practice's switch, settings and last outcome for one integration.

    A row exists once the integration has been changed or has run; without one the integration
    has its defaults (the central ones on, practice management and bookkeeping off).
    """

    __tablename__ = "integration_connections"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "integration",
            name="uq_integration_connections_organization_id_integration",
        ),
        CheckConstraint(f"integration IN ({_INTEGRATION_KEYS_SQL})", name="integration"),
        CheckConstraint("status IN ('idle', 'ok', 'error')", name="status"),
        CheckConstraint(
            "last_error IS NULL OR length(last_error) <= 1000", name="last_error_length"
        ),
    )

    integration: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # How the last attempt went: ``idle`` (nothing yet), ``ok`` or ``error``.
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="idle")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class IntegrationSyncItem(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """One thing to send to (or apply from) another system: the outbox.

    ``idempotency_key`` is unique per practice and integration, so the same document or change
    is never queued twice. ``pending`` items wait for ``next_attempt_at`` (a failed attempt
    schedules the next one with a backoff); ``failed`` ones need a person (a retry) and ``ok``
    ones carry the other system's reference in ``external_ref``.
    """

    __tablename__ = "integration_sync_items"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "integration",
            "idempotency_key",
            name="uq_integration_sync_items_organization_id_idempotency_key",
        ),
        CheckConstraint(f"integration IN ({_INTEGRATION_KEYS_SQL})", name="integration"),
        CheckConstraint("direction IN ('inbound', 'outbound')", name="direction"),
        CheckConstraint("length(operation) BETWEEN 1 AND 60", name="operation_length"),
        CheckConstraint("length(idempotency_key) BETWEEN 1 AND 200", name="idempotency_key_length"),
        CheckConstraint("status IN ('pending', 'ok', 'failed', 'skipped')", name="status"),
        CheckConstraint("attempts >= 0", name="attempts"),
        CheckConstraint(
            "(status = 'pending') = (next_attempt_at IS NOT NULL)", name="next_attempt"
        ),
        CheckConstraint(
            "last_error IS NULL OR length(last_error) <= 1000", name="last_error_length"
        ),
        Index(
            "ix_integration_sync_items_due",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_integration_sync_items_org_integration_status",
            "organization_id",
            "integration",
            "status",
            "created_at",
        ),
        Index(
            "ix_integration_sync_items_organization_id_entity_id",
            "organization_id",
            "entity_id",
            postgresql_where=text("entity_id IS NOT NULL"),
        ),
    )

    integration: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    direction: Mapped[str] = mapped_column(Text, nullable=False, server_default="outbound")
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Set while pending (when the next attempt is due), null otherwise.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    # A stable code for the last failure: UNMAPPED, UNAVAILABLE, REJECTED, INVALID_TRANSITION ...
    last_error_code: Mapped[str | None] = mapped_column(Text)
    external_ref: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class BookkeepingServiceMapping(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """The income account a service's invoice lines are booked to. ``service_id`` null is the
    account for lines that have no service (manual lines, disbursements)."""

    __tablename__ = "bookkeeping_service_mappings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_bk_service_mappings_service",
            ondelete="CASCADE",
        ),
        CheckConstraint(f"account_code ~ '{CODE_PATTERN}'", name="account_code_format"),
        CheckConstraint(
            "account_name IS NULL OR length(account_name) BETWEEN 1 AND 120",
            name="account_name_length",
        ),
        Index(
            "uq_bk_service_mappings_service",
            "organization_id",
            "service_id",
            unique=True,
            postgresql_where=text("service_id IS NOT NULL"),
        ),
        Index(
            "uq_bk_service_mappings_default",
            "organization_id",
            unique=True,
            postgresql_where=text("service_id IS NULL"),
        ),
    )

    service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    account_code: Mapped[str] = mapped_column(Text, nullable=False)
    account_name: Mapped[str | None] = mapped_column(Text)


class BookkeepingVatMapping(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """The tax code a VAT code is booked to. ``vat_rate_id`` null is the tax code for lines
    that carry no VAT code (a practice that is not VAT registered)."""

    __tablename__ = "bookkeeping_vat_mappings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_bk_vat_mappings_vat_rate",
            ondelete="CASCADE",
        ),
        CheckConstraint(f"tax_code ~ '{CODE_PATTERN}'", name="tax_code_format"),
        CheckConstraint(
            "tax_name IS NULL OR length(tax_name) BETWEEN 1 AND 120", name="tax_name_length"
        ),
        Index(
            "uq_bk_vat_mappings_vat_rate",
            "organization_id",
            "vat_rate_id",
            unique=True,
            postgresql_where=text("vat_rate_id IS NOT NULL"),
        ),
        Index(
            "uq_bk_vat_mappings_default",
            "organization_id",
            unique=True,
            postgresql_where=text("vat_rate_id IS NULL"),
        ),
    )

    vat_rate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tax_code: Mapped[str] = mapped_column(Text, nullable=False)
    tax_name: Mapped[str | None] = mapped_column(Text)


__all__ = [
    "CENTRAL_CLIENT_STATUSES",
    "CODE_PATTERN",
    "CONNECTION_STATUSES",
    "INTEGRATION_KEYS",
    "SYNC_DIRECTIONS",
    "SYNC_ITEM_STATUSES",
    "SYNC_STATUSES",
    "BookkeepingServiceMapping",
    "BookkeepingVatMapping",
    "CentralClientCache",
    "IntegrationConnection",
    "IntegrationCursor",
    "IntegrationSyncItem",
    "IntegrationSyncLog",
]
