"""Billing workflow records: invoice approval history, payment reminders and statement sends.

``invoice_approvals`` is the append-only history of an invoice's approval (submitted, approved,
rejected). ``payment_reminders`` logs every reminder about an overdue invoice (one email lists a
client's invoices; each invoice gets a row and the rows of one email share a ``batch_id``) and
``statement_sends`` every client statement queued for email.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin

APPROVAL_ACTIONS = ("submitted", "approved", "rejected")
REMINDER_STATUSES = ("queued", "sent", "failed", "skipped")
REMINDER_SOURCES = ("manual", "scheduled")
SEND_STATUSES = ("queued", "sent", "failed")


class InvoiceApproval(Base, IdMixin, OrgScopedMixin):
    """One step of an invoice's approval. Rows are only ever added."""

    __tablename__ = "invoice_approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name="fk_invoice_approvals_invoice",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_invoice_approvals_organization_id_actor_members",
        ),
        CheckConstraint("action IN ('submitted', 'approved', 'rejected')", name="action"),
        CheckConstraint("note IS NULL OR length(note) BETWEEN 1 AND 500", name="note_length"),
        CheckConstraint("action <> 'rejected' OR note IS NOT NULL", name="rejection_has_reason"),
        CheckConstraint("gross_total >= 0", name="gross_total"),
        Index("ix_invoice_approvals_invoice_id_created_at", "invoice_id", "created_at", "id"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    # The invoice total when the step happened: an approval is of this amount.
    gross_total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PaymentReminder(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A reminder to a client about one overdue invoice.

    The client is the invoice's. At most one reminder per invoice and day is queued or sent (a
    failed or skipped attempt does not use the day up); ``sent_on`` is that day in the practice's
    time zone. ``sent_by`` is null for the scheduled job.
    """

    __tablename__ = "payment_reminders"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_payment_reminders_organization_id_id"),
        ForeignKeyConstraint(
            ["organization_id", "invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name="fk_payment_reminders_invoice",
        ),
        CheckConstraint("source IN ('manual', 'scheduled')", name="source"),
        CheckConstraint("status IN ('queued', 'sent', 'failed', 'skipped')", name="status"),
        CheckConstraint("length(template) BETWEEN 1 AND 80", name="template_length"),
        CheckConstraint("(source = 'scheduled') = (sent_by IS NULL)", name="sender"),
        CheckConstraint("(status = 'sent') = (sent_at IS NOT NULL)", name="sent_at"),
        CheckConstraint("cardinality(recipients) >= 1", name="has_recipients"),
        CheckConstraint("days_overdue >= 0 AND amount_due >= 0", name="figures"),
        Index(
            "uq_payment_reminders_organization_id_invoice_id_sent_on",
            "organization_id",
            "invoice_id",
            "sent_on",
            unique=True,
            postgresql_where=text("status IN ('queued', 'sent')"),
        ),
        Index(
            "ix_payment_reminders_organization_id_created_at", "organization_id", "created_at", "id"
        ),
        Index("ix_payment_reminders_invoice_id", "invoice_id"),
        Index("ix_payment_reminders_batch_id", "batch_id"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The reminders that went out in one email.
    batch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    sent_on: Mapped[date] = mapped_column(Date, nullable=False)
    recipients: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    days_overdue: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_due: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sent_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class StatementSend(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A client statement queued for email: the period, the recipients and the closing balance."""

    __tablename__ = "statement_sends"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_statement_sends_organization_id_id"),
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_statement_sends_organization_id_client_id_clients",
        ),
        CheckConstraint("status IN ('queued', 'sent', 'failed')", name="status"),
        CheckConstraint("date_from IS NULL OR date_from <= date_to", name="period"),
        CheckConstraint("cardinality(recipients) >= 1", name="has_recipients"),
        CheckConstraint("(status = 'sent') = (sent_at IS NOT NULL)", name="sent_at"),
        Index(
            "ix_statement_sends_organization_id_client_id_created_at",
            "organization_id",
            "client_id",
            "created_at",
        ),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    date_from: Mapped[date | None] = mapped_column(Date)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    recipients: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    # What the client owed (negative: was owed to the client) when the statement was queued.
    closing_balance: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    sent_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


__all__ = [
    "APPROVAL_ACTIONS",
    "REMINDER_SOURCES",
    "REMINDER_STATUSES",
    "SEND_STATUSES",
    "InvoiceApproval",
    "PaymentReminder",
    "StatementSend",
]
