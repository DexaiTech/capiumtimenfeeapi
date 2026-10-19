"""Disbursements, work-in-progress allocation, invoices, credit notes and payments.

How WIP flows (see docs/billing.md):

* Billable time entries and disbursements start ``unbilled``: that is the WIP ledger.
* Putting them on a draft invoice creates ``wip_allocations`` rows tied to an invoice line
  and moves them to ``in_draft``; deleting the draft (or removing the line) reverses that.
* Issuing the invoice moves them to ``billed``. The billed value per item may differ from
  its WIP value: the difference is the write-up (+) or write-off (-) of that item.
* Voiding an issued invoice reverses its allocations (``reversed_at``) and puts the items
  back in WIP. A credit note does not reinstate WIP.
* A WIP write-off without an invoice allocates items to a ``wip_write_offs`` row with a
  billed value of zero and moves them to ``written_off``.

Invoice totals are kept on the invoice row and checked by constraints; ``paid_total`` and
``credited_total`` are maintained under the invoice row lock whenever a payment allocation
or credit note changes.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

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
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, TimestampMixin

INVOICE_STATUSES = ("draft", "awaiting_approval", "approved", "issued", "void")
LINE_KINDS = ("time", "fixed_fee", "disbursement", "manual")
PAYMENT_METHODS = ("bank_transfer", "card", "direct_debit", "cash", "cheque", "other")


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


class Disbursement(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A cost paid on a client's behalf (Companies House fee, travel, ...) to recharge."""

    __tablename__ = "disbursements"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_disbursements_organization_id_id"),
        _client_fk("disbursements"),
        ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_disbursements_organization_id_client_id_job_id_jobs",
        ),
        _vat_fk("disbursements"),
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint("length(description) BETWEEN 1 AND 500", name="description_length"),
        CheckConstraint(
            "billing_status IN ('unbilled', 'in_draft', 'billed', 'written_off')",
            name="billing_status",
        ),
        CheckConstraint("is_billable OR billing_status = 'unbilled'", name="non_billable_status"),
        Index(
            "ix_disbursements_organization_id_client_id_incurred_on",
            "organization_id",
            "client_id",
            "incurred_on",
        ),
        Index(
            "ix_disbursements_wip",
            "organization_id",
            "client_id",
            postgresql_where=text("is_billable AND billing_status = 'unbilled'"),
        ),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    incurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    supplier: Mapped[str | None] = mapped_column(Text)
    # Net amount to recharge, in minor units.
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # VAT charged when it is billed; null uses the practice default.
    vat_rate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    is_billable: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    billing_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="unbilled")
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class Invoice(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_invoices_organization_id_id"),
        UniqueConstraint(
            "organization_id", "client_id", "id", name="uq_invoices_organization_id_client_id_id"
        ),
        _client_fk("invoices"),
        CheckConstraint(
            "status IN ('draft', 'awaiting_approval', 'approved', 'issued', 'void')",
            name="status",
        ),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_format"),
        CheckConstraint(
            "(status IN ('draft', 'awaiting_approval', 'approved')) = (number IS NULL)",
            name="number_when_issued",
        ),
        CheckConstraint(
            "status IN ('draft', 'awaiting_approval', 'approved') "
            "OR (issue_date IS NOT NULL AND due_date IS NOT NULL)",
            name="dates_when_issued",
        ),
        CheckConstraint(
            "due_date IS NULL OR issue_date IS NULL OR due_date >= issue_date",
            name="due_after_issue",
        ),
        CheckConstraint("gross_total = net_total + vat_total", name="gross_total"),
        CheckConstraint("gross_total >= 0", name="gross_non_negative"),
        CheckConstraint("paid_total >= 0 AND credited_total >= 0", name="settled_non_negative"),
        CheckConstraint("paid_total + credited_total <= gross_total", name="not_over_settled"),
        CheckConstraint("status <> 'void' OR paid_total = 0", name="void_has_no_payments"),
        Index(
            "uq_invoices_organization_id_number",
            "organization_id",
            "number",
            unique=True,
            postgresql_where=text("number IS NOT NULL"),
        ),
        Index("ix_invoices_organization_id_client_id", "organization_id", "client_id"),
        Index(
            "ix_invoices_organization_id_status_due_date", "organization_id", "status", "due_date"
        ),
        Index("ix_invoices_organization_id_issue_date", "organization_id", "issue_date"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Assigned from the "invoice" sequence when issued; drafts have none.
    number: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    issue_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    reference: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    net_total: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    vat_total: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    gross_total: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    paid_total: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    credited_total: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    issued_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    void_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))

    @property
    def balance(self) -> int:
        """What the client still owes on this invoice."""
        if self.status != "issued":
            return 0
        return self.gross_total - self.paid_total - self.credited_total


class InvoiceLine(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "invoice_lines"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_invoice_lines_organization_id_id"),
        ForeignKeyConstraint(
            ["organization_id", "invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name="fk_invoice_lines_organization_id_invoice_id_invoices",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            name="fk_invoice_lines_organization_id_job_id_jobs",
        ),
        ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_invoice_lines_organization_id_service_id_services",
        ),
        _vat_fk("invoice_lines"),
        CheckConstraint("kind IN ('time', 'fixed_fee', 'disbursement', 'manual')", name="kind"),
        CheckConstraint("quantity > 0", name="quantity"),
        CheckConstraint("vat_rate_bp BETWEEN 0 AND 10000", name="vat_rate_bp"),
        CheckConstraint("length(description) BETWEEN 1 AND 1000", name="description_length"),
        Index("ix_invoice_lines_invoice_id_position", "invoice_id", "position"),
    )

    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default=text("1")
    )
    # Price of one unit; net = round(quantity x unit_amount). Negative for a discount line.
    unit_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vat_rate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Snapshot of the rate when the line was written: later rate changes never alter it.
    vat_rate_bp: Mapped[int] = mapped_column(Integer, nullable=False)
    vat_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)


class WipWriteOff(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """Unbilled work taken out of WIP without an invoice."""

    __tablename__ = "wip_write_offs"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_wip_write_offs_organization_id_id"),
        _client_fk("wip_write_offs"),
        ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_wip_write_offs_organization_id_client_id_job_id_jobs",
        ),
        CheckConstraint("total_wip_value >= 0", name="total_wip_value"),
        CheckConstraint("length(reason) BETWEEN 1 AND 500", name="reason_length"),
        Index("ix_wip_write_offs_organization_id_client_id", "organization_id", "client_id"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    written_off_on: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    total_wip_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class WipAllocation(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """Where one WIP item went: an invoice line or a write-off.

    Exactly one item (time entry, disbursement or expense line) and exactly one destination.
    A live (not reversed) allocation per item is enforced by partial unique indexes.
    ``billed_value - wip_value`` is the item's write-up (positive) or write-off (negative).
    """

    __tablename__ = "wip_allocations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "time_entry_id"],
            ["time_entries.organization_id", "time_entries.id"],
            name="fk_wip_allocations_time_entry",
        ),
        ForeignKeyConstraint(
            ["organization_id", "disbursement_id"],
            ["disbursements.organization_id", "disbursements.id"],
            name="fk_wip_allocations_disbursement",
        ),
        ForeignKeyConstraint(
            ["organization_id", "expense_id"],
            ["expenses.organization_id", "expenses.id"],
            name="fk_wip_allocations_expense",
        ),
        ForeignKeyConstraint(
            ["organization_id", "invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name="fk_wip_allocations_invoice",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "invoice_line_id"],
            ["invoice_lines.organization_id", "invoice_lines.id"],
            name="fk_wip_allocations_invoice_line",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "write_off_id"],
            ["wip_write_offs.organization_id", "wip_write_offs.id"],
            name="fk_wip_allocations_write_off",
        ),
        CheckConstraint(
            "num_nonnulls(time_entry_id, disbursement_id, expense_id) = 1", name="one_item"
        ),
        CheckConstraint(
            "(invoice_line_id IS NULL) <> (write_off_id IS NULL)", name="one_destination"
        ),
        CheckConstraint(
            "(invoice_line_id IS NULL) = (invoice_id IS NULL)", name="line_has_invoice"
        ),
        CheckConstraint("wip_value >= 0 AND billed_value >= 0", name="values"),
        CheckConstraint("write_off_id IS NULL OR billed_value = 0", name="write_off_bills_zero"),
        Index(
            "uq_wip_allocations_time_entry_live",
            "time_entry_id",
            unique=True,
            postgresql_where=text("time_entry_id IS NOT NULL AND reversed_at IS NULL"),
        ),
        Index(
            "uq_wip_allocations_disbursement_live",
            "disbursement_id",
            unique=True,
            postgresql_where=text("disbursement_id IS NOT NULL AND reversed_at IS NULL"),
        ),
        Index(
            "uq_wip_allocations_expense_live",
            "expense_id",
            unique=True,
            postgresql_where=text("expense_id IS NOT NULL AND reversed_at IS NULL"),
        ),
        Index("ix_wip_allocations_invoice_id", "invoice_id"),
        Index("ix_wip_allocations_invoice_line_id", "invoice_line_id"),
        Index("ix_wip_allocations_write_off_id", "write_off_id"),
    )

    time_entry_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    disbursement_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # An approved billable expense claim line (docs/modules/wip.md, "Contract for billing").
    expense_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    invoice_line_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    write_off_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    wip_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    billed_value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CreditNote(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """Reduces what is owed on one issued invoice. Issued (numbered) on creation."""

    __tablename__ = "credit_notes"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_credit_notes_organization_id_id"),
        _client_fk("credit_notes"),
        ForeignKeyConstraint(
            ["organization_id", "client_id", "invoice_id"],
            ["invoices.organization_id", "invoices.client_id", "invoices.id"],
            name="fk_credit_notes_organization_id_client_id_invoice_id_invoices",
        ),
        CheckConstraint("gross_total = net_total + vat_total", name="gross_total"),
        CheckConstraint("gross_total > 0", name="gross_positive"),
        CheckConstraint("length(reason) BETWEEN 1 AND 500", name="reason_length"),
        Index("uq_credit_notes_organization_id_number", "organization_id", "number", unique=True),
        Index("ix_credit_notes_organization_id_invoice_id", "organization_id", "invoice_id"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    number: Mapped[str] = mapped_column(Text, nullable=False)
    issue_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    net_total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vat_total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gross_total: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class CreditNoteLine(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "credit_note_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "credit_note_id"],
            ["credit_notes.organization_id", "credit_notes.id"],
            name="fk_credit_note_lines_credit_note",
            ondelete="CASCADE",
        ),
        _vat_fk("credit_note_lines"),
        CheckConstraint("net_amount > 0", name="net_amount"),
        CheckConstraint("vat_rate_bp BETWEEN 0 AND 10000", name="vat_rate_bp"),
        CheckConstraint("vat_amount >= 0", name="vat_amount"),
        CheckConstraint("length(description) BETWEEN 1 AND 1000", name="description_length"),
        Index("ix_credit_note_lines_credit_note_id", "credit_note_id"),
    )

    credit_note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    net_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    vat_rate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    vat_rate_bp: Mapped[int] = mapped_column(Integer, nullable=False)
    vat_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Payment(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """Money received from a client. Allocated to invoices; the rest is on account."""

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_payments_organization_id_id"),
        UniqueConstraint(
            "organization_id", "client_id", "id", name="uq_payments_organization_id_client_id_id"
        ),
        _client_fk("payments"),
        CheckConstraint("amount > 0", name="amount"),
        CheckConstraint(
            "method IN ('bank_transfer', 'card', 'direct_debit', 'cash', 'cheque', 'other')",
            name="method",
        ),
        CheckConstraint(
            "allocated_total >= 0 AND allocated_total <= amount", name="allocated_total"
        ),
        CheckConstraint("voided_at IS NULL OR allocated_total = 0", name="void_unallocated"),
        # A reversal (bounced payment) is also void, so every "live payment" rule still holds.
        CheckConstraint(
            "(reversed_at IS NULL) = (reversed_by IS NULL) "
            "AND (reversed_at IS NULL) = (reversal_reason IS NULL)",
            name="reversal_fields",
        ),
        CheckConstraint("reversed_at IS NULL OR voided_at IS NOT NULL", name="reversed_is_void"),
        CheckConstraint("NOT bounced OR reversed_at IS NOT NULL", name="bounced_is_reversed"),
        CheckConstraint(
            "reversal_reason IS NULL OR length(reversal_reason) BETWEEN 1 AND 500",
            name="reversal_reason_length",
        ),
        Index(
            "ix_payments_organization_id_client_id_received_on",
            "organization_id",
            "client_id",
            "received_on",
        ),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    received_on: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False, server_default="bank_transfer")
    reference: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    allocated_total: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    reversal_reason: Mapped[str | None] = mapped_column(Text)
    # True when the money never arrived (a bounced cheque or a returned direct debit).
    bounced: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))

    @property
    def unallocated(self) -> int:
        return 0 if self.voided_at else self.amount - self.allocated_total


class PaymentAllocation(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    __tablename__ = "payment_allocations"
    __table_args__ = (
        # Payment and invoice must belong to the same client.
        ForeignKeyConstraint(
            ["organization_id", "client_id", "payment_id"],
            ["payments.organization_id", "payments.client_id", "payments.id"],
            name="fk_payment_allocations_payment",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "client_id", "invoice_id"],
            ["invoices.organization_id", "invoices.client_id", "invoices.id"],
            name="fk_payment_allocations_invoice",
        ),
        CheckConstraint("amount > 0", name="amount"),
        UniqueConstraint(
            "payment_id", "invoice_id", name="uq_payment_allocations_payment_id_invoice_id"
        ),
        Index("ix_payment_allocations_invoice_id", "invoice_id"),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    invoice_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)


__all__ = [
    "CreditNote",
    "CreditNoteLine",
    "Disbursement",
    "Invoice",
    "InvoiceLine",
    "Payment",
    "PaymentAllocation",
    "WipAllocation",
    "WipWriteOff",
]
