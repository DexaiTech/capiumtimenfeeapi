"""create billing

Revision ID: 005
Revises: 004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGER_TABLES = (
    "disbursements",
    "invoices",
    "invoice_lines",
    "wip_write_offs",
    "wip_allocations",
    "credit_notes",
    "credit_note_lines",
    "payments",
    "payment_allocations",
)


def upgrade() -> None:
    op.create_table(
        "invoices",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("number", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("net_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("vat_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("gross_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("paid_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("credited_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issued_by", sa.UUID(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_by", sa.UUID(), nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'draft') = (number IS NULL)", name=op.f("ck_invoices_number_when_issued")
        ),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_invoices_currency_format")),
        sa.CheckConstraint(
            "status <> 'void' OR paid_total = 0", name=op.f("ck_invoices_void_has_no_payments")
        ),
        sa.CheckConstraint(
            "status = 'draft' OR (issue_date IS NOT NULL AND due_date IS NOT NULL)",
            name=op.f("ck_invoices_dates_when_issued"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'issued', 'void')", name=op.f("ck_invoices_status")
        ),
        sa.CheckConstraint(
            "due_date IS NULL OR issue_date IS NULL OR due_date >= issue_date",
            name=op.f("ck_invoices_due_after_issue"),
        ),
        sa.CheckConstraint(
            "gross_total = net_total + vat_total", name=op.f("ck_invoices_gross_total")
        ),
        sa.CheckConstraint("gross_total >= 0", name=op.f("ck_invoices_gross_non_negative")),
        sa.CheckConstraint(
            "paid_total + credited_total <= gross_total", name=op.f("ck_invoices_not_over_settled")
        ),
        sa.CheckConstraint(
            "paid_total >= 0 AND credited_total >= 0", name=op.f("ck_invoices_settled_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_invoices_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["issued_by"], ["users.id"], name=op.f("fk_invoices_issued_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_invoices_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_invoices_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["voided_by"], ["users.id"], name=op.f("fk_invoices_voided_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoices")),
        sa.UniqueConstraint(
            "organization_id", "client_id", "id", name="uq_invoices_organization_id_client_id_id"
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_invoices_organization_id_id"),
    )
    op.create_index(
        "ix_invoices_organization_id_client_id",
        "invoices",
        ["organization_id", "client_id"],
        unique=False,
    )
    op.create_index(
        "ix_invoices_organization_id_issue_date",
        "invoices",
        ["organization_id", "issue_date"],
        unique=False,
    )
    op.create_index(
        "ix_invoices_organization_id_status_due_date",
        "invoices",
        ["organization_id", "status", "due_date"],
        unique=False,
    )
    op.create_index(
        "uq_invoices_organization_id_number",
        "invoices",
        ["organization_id", "number"],
        unique=True,
        postgresql_where=sa.text("number IS NOT NULL"),
    )
    op.create_table(
        "payments",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("received_on", sa.Date(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.Text(), server_default="bank_transfer", nullable=False),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("allocated_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "method IN ('bank_transfer', 'card', 'direct_debit', 'cash', 'cheque', 'other')",
            name=op.f("ck_payments_method"),
        ),
        sa.CheckConstraint(
            "allocated_total >= 0 AND allocated_total <= amount",
            name=op.f("ck_payments_allocated_total"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payments_amount")),
        sa.CheckConstraint(
            "voided_at IS NULL OR allocated_total = 0", name=op.f("ck_payments_void_unallocated")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_payments_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_payments_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_payments_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payments")),
        sa.UniqueConstraint(
            "organization_id", "client_id", "id", name="uq_payments_organization_id_client_id_id"
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_payments_organization_id_id"),
    )
    op.create_index(
        "ix_payments_organization_id_client_id_received_on",
        "payments",
        ["organization_id", "client_id", "received_on"],
        unique=False,
    )
    op.create_table(
        "credit_notes",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("invoice_id", sa.UUID(), nullable=False),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("net_total", sa.BigInteger(), nullable=False),
        sa.Column("vat_total", sa.BigInteger(), nullable=False),
        sa.Column("gross_total", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "gross_total = net_total + vat_total", name=op.f("ck_credit_notes_gross_total")
        ),
        sa.CheckConstraint("gross_total > 0", name=op.f("ck_credit_notes_gross_positive")),
        sa.CheckConstraint(
            "length(reason) BETWEEN 1 AND 500", name=op.f("ck_credit_notes_reason_length")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_credit_notes_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "invoice_id"],
            ["invoices.organization_id", "invoices.client_id", "invoices.id"],
            name="fk_credit_notes_organization_id_client_id_invoice_id_invoices",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_credit_notes_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_credit_notes_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_notes")),
        sa.UniqueConstraint("organization_id", "id", name="uq_credit_notes_organization_id_id"),
    )
    op.create_index(
        "ix_credit_notes_organization_id_invoice_id",
        "credit_notes",
        ["organization_id", "invoice_id"],
        unique=False,
    )
    op.create_index(
        "uq_credit_notes_organization_id_number",
        "credit_notes",
        ["organization_id", "number"],
        unique=True,
    )
    op.create_table(
        "disbursements",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("incurred_on", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("supplier", sa.Text(), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("vat_rate_id", sa.UUID(), nullable=True),
        sa.Column("is_billable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("billing_status", sa.Text(), server_default="unbilled", nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "billing_status IN ('unbilled', 'in_draft', 'billed', 'written_off')",
            name=op.f("ck_disbursements_billing_status"),
        ),
        sa.CheckConstraint(
            "is_billable OR billing_status = 'unbilled'",
            name=op.f("ck_disbursements_non_billable_status"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_disbursements_amount")),
        sa.CheckConstraint(
            "length(description) BETWEEN 1 AND 500",
            name=op.f("ck_disbursements_description_length"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_disbursements_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_disbursements_organization_id_client_id_job_id_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_disbursements_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_disbursements_organization_id_vat_rate_id_vat_rates",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_disbursements_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_disbursements")),
        sa.UniqueConstraint("organization_id", "id", name="uq_disbursements_organization_id_id"),
    )
    op.create_index(
        "ix_disbursements_organization_id_client_id_incurred_on",
        "disbursements",
        ["organization_id", "client_id", "incurred_on"],
        unique=False,
    )
    op.create_index(
        "ix_disbursements_wip",
        "disbursements",
        ["organization_id", "client_id"],
        unique=False,
        postgresql_where=sa.text("is_billable AND billing_status = 'unbilled'"),
    )
    op.create_table(
        "invoice_lines",
        sa.Column("invoice_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("service_id", sa.UUID(), nullable=True),
        sa.Column(
            "quantity",
            sa.Numeric(precision=12, scale=2),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("unit_amount", sa.BigInteger(), nullable=False),
        sa.Column("net_amount", sa.BigInteger(), nullable=False),
        sa.Column("vat_rate_id", sa.UUID(), nullable=True),
        sa.Column("vat_rate_bp", sa.Integer(), nullable=False),
        sa.Column("vat_amount", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('time', 'fixed_fee', 'disbursement', 'manual')",
            name=op.f("ck_invoice_lines_kind"),
        ),
        sa.CheckConstraint(
            "length(description) BETWEEN 1 AND 1000",
            name=op.f("ck_invoice_lines_description_length"),
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_invoice_lines_quantity")),
        sa.CheckConstraint(
            "vat_rate_bp BETWEEN 0 AND 10000", name=op.f("ck_invoice_lines_vat_rate_bp")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name="fk_invoice_lines_organization_id_invoice_id_invoices",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            name="fk_invoice_lines_organization_id_job_id_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_invoice_lines_organization_id_service_id_services",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_invoice_lines_organization_id_vat_rate_id_vat_rates",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_invoice_lines_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_lines")),
        sa.UniqueConstraint("organization_id", "id", name="uq_invoice_lines_organization_id_id"),
    )
    op.create_index(
        "ix_invoice_lines_invoice_id_position",
        "invoice_lines",
        ["invoice_id", "position"],
        unique=False,
    )
    op.create_table(
        "payment_allocations",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("payment_id", sa.UUID(), nullable=False),
        sa.Column("invoice_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payment_allocations_amount")),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "invoice_id"],
            ["invoices.organization_id", "invoices.client_id", "invoices.id"],
            name="fk_payment_allocations_invoice",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "payment_id"],
            ["payments.organization_id", "payments.client_id", "payments.id"],
            name="fk_payment_allocations_payment",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_payment_allocations_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_allocations")),
        sa.UniqueConstraint(
            "payment_id", "invoice_id", name="uq_payment_allocations_payment_id_invoice_id"
        ),
    )
    op.create_index(
        "ix_payment_allocations_invoice_id", "payment_allocations", ["invoice_id"], unique=False
    )
    op.create_table(
        "wip_write_offs",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("written_off_on", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("total_wip_value", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(reason) BETWEEN 1 AND 500", name=op.f("ck_wip_write_offs_reason_length")
        ),
        sa.CheckConstraint("total_wip_value >= 0", name=op.f("ck_wip_write_offs_total_wip_value")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_wip_write_offs_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_wip_write_offs_organization_id_client_id_job_id_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_wip_write_offs_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_wip_write_offs_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wip_write_offs")),
        sa.UniqueConstraint("organization_id", "id", name="uq_wip_write_offs_organization_id_id"),
    )
    op.create_index(
        "ix_wip_write_offs_organization_id_client_id",
        "wip_write_offs",
        ["organization_id", "client_id"],
        unique=False,
    )
    op.create_table(
        "credit_note_lines",
        sa.Column("credit_note_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("net_amount", sa.BigInteger(), nullable=False),
        sa.Column("vat_rate_id", sa.UUID(), nullable=True),
        sa.Column("vat_rate_bp", sa.Integer(), nullable=False),
        sa.Column("vat_amount", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(description) BETWEEN 1 AND 1000",
            name=op.f("ck_credit_note_lines_description_length"),
        ),
        sa.CheckConstraint("net_amount > 0", name=op.f("ck_credit_note_lines_net_amount")),
        sa.CheckConstraint("vat_amount >= 0", name=op.f("ck_credit_note_lines_vat_amount")),
        sa.CheckConstraint(
            "vat_rate_bp BETWEEN 0 AND 10000", name=op.f("ck_credit_note_lines_vat_rate_bp")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "credit_note_id"],
            ["credit_notes.organization_id", "credit_notes.id"],
            name="fk_credit_note_lines_credit_note",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_credit_note_lines_organization_id_vat_rate_id_vat_rates",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_credit_note_lines_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_note_lines")),
    )
    op.create_index(
        "ix_credit_note_lines_credit_note_id", "credit_note_lines", ["credit_note_id"], unique=False
    )
    op.create_table(
        "wip_allocations",
        sa.Column("time_entry_id", sa.UUID(), nullable=True),
        sa.Column("disbursement_id", sa.UUID(), nullable=True),
        sa.Column("invoice_id", sa.UUID(), nullable=True),
        sa.Column("invoice_line_id", sa.UUID(), nullable=True),
        sa.Column("write_off_id", sa.UUID(), nullable=True),
        sa.Column("wip_value", sa.BigInteger(), nullable=False),
        sa.Column("billed_value", sa.BigInteger(), nullable=False),
        sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(invoice_line_id IS NULL) <> (write_off_id IS NULL)",
            name=op.f("ck_wip_allocations_one_destination"),
        ),
        sa.CheckConstraint(
            "(invoice_line_id IS NULL) = (invoice_id IS NULL)",
            name=op.f("ck_wip_allocations_line_has_invoice"),
        ),
        sa.CheckConstraint(
            "(time_entry_id IS NULL) <> (disbursement_id IS NULL)",
            name=op.f("ck_wip_allocations_one_item"),
        ),
        sa.CheckConstraint(
            "wip_value >= 0 AND billed_value >= 0", name=op.f("ck_wip_allocations_values")
        ),
        sa.CheckConstraint(
            "write_off_id IS NULL OR billed_value = 0",
            name=op.f("ck_wip_allocations_write_off_bills_zero"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "disbursement_id"],
            ["disbursements.organization_id", "disbursements.id"],
            name="fk_wip_allocations_disbursement",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name="fk_wip_allocations_invoice",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "invoice_line_id"],
            ["invoice_lines.organization_id", "invoice_lines.id"],
            name="fk_wip_allocations_invoice_line",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "time_entry_id"],
            ["time_entries.organization_id", "time_entries.id"],
            name="fk_wip_allocations_time_entry",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "write_off_id"],
            ["wip_write_offs.organization_id", "wip_write_offs.id"],
            name="fk_wip_allocations_write_off",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_wip_allocations_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wip_allocations")),
    )
    op.create_index(
        "ix_wip_allocations_invoice_id", "wip_allocations", ["invoice_id"], unique=False
    )
    op.create_index(
        "ix_wip_allocations_invoice_line_id", "wip_allocations", ["invoice_line_id"], unique=False
    )
    op.create_index(
        "ix_wip_allocations_write_off_id", "wip_allocations", ["write_off_id"], unique=False
    )
    op.create_index(
        "uq_wip_allocations_disbursement_live",
        "wip_allocations",
        ["disbursement_id"],
        unique=True,
        postgresql_where=sa.text("disbursement_id IS NOT NULL AND reversed_at IS NULL"),
    )
    op.create_index(
        "uq_wip_allocations_time_entry_live",
        "wip_allocations",
        ["time_entry_id"],
        unique=True,
        postgresql_where=sa.text("time_entry_id IS NOT NULL AND reversed_at IS NULL"),
    )
    add_updated_at_triggers(*TRIGGER_TABLES)


def downgrade() -> None:
    op.drop_index(
        "uq_wip_allocations_time_entry_live",
        table_name="wip_allocations",
        postgresql_where=sa.text("time_entry_id IS NOT NULL AND reversed_at IS NULL"),
    )
    op.drop_index(
        "uq_wip_allocations_disbursement_live",
        table_name="wip_allocations",
        postgresql_where=sa.text("disbursement_id IS NOT NULL AND reversed_at IS NULL"),
    )
    op.drop_index("ix_wip_allocations_write_off_id", table_name="wip_allocations")
    op.drop_index("ix_wip_allocations_invoice_line_id", table_name="wip_allocations")
    op.drop_index("ix_wip_allocations_invoice_id", table_name="wip_allocations")
    op.drop_table("wip_allocations")
    op.drop_index("ix_credit_note_lines_credit_note_id", table_name="credit_note_lines")
    op.drop_table("credit_note_lines")
    op.drop_index("ix_wip_write_offs_organization_id_client_id", table_name="wip_write_offs")
    op.drop_table("wip_write_offs")
    op.drop_index("ix_payment_allocations_invoice_id", table_name="payment_allocations")
    op.drop_table("payment_allocations")
    op.drop_index("ix_invoice_lines_invoice_id_position", table_name="invoice_lines")
    op.drop_table("invoice_lines")
    op.drop_index(
        "ix_disbursements_wip",
        table_name="disbursements",
        postgresql_where=sa.text("is_billable AND billing_status = 'unbilled'"),
    )
    op.drop_index(
        "ix_disbursements_organization_id_client_id_incurred_on", table_name="disbursements"
    )
    op.drop_table("disbursements")
    op.drop_index("uq_credit_notes_organization_id_number", table_name="credit_notes")
    op.drop_index("ix_credit_notes_organization_id_invoice_id", table_name="credit_notes")
    op.drop_table("credit_notes")
    op.drop_index("ix_payments_organization_id_client_id_received_on", table_name="payments")
    op.drop_table("payments")
    op.drop_index(
        "uq_invoices_organization_id_number",
        table_name="invoices",
        postgresql_where=sa.text("number IS NOT NULL"),
    )
    op.drop_index("ix_invoices_organization_id_status_due_date", table_name="invoices")
    op.drop_index("ix_invoices_organization_id_issue_date", table_name="invoices")
    op.drop_index("ix_invoices_organization_id_client_id", table_name="invoices")
    op.drop_table("invoices")
