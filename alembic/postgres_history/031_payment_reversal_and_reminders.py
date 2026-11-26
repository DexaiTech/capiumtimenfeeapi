"""payment reversal, payment reminders and the reminder settings

Adds ``payments.reversed_at/reversed_by/reversal_reason/bounced`` (a reversed payment is also void:
``voided_at`` is set with it), the ``payment_reminders`` log (one row per reminder about one overdue
invoice; at most one queued or sent per invoice and day) and the practice settings
``payment_reminders_enabled`` / ``payment_reminder_days_after_due``.

Revision ID: 031
Revises: 030
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "031"
down_revision: str | None = "030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("reversed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payments", sa.Column("reversed_by", sa.UUID(), nullable=True))
    op.add_column("payments", sa.Column("reversal_reason", sa.Text(), nullable=True))
    op.add_column(
        "payments",
        sa.Column("bounced", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_foreign_key(
        op.f("fk_payments_reversed_by_users"), "payments", "users", ["reversed_by"], ["id"]
    )
    op.create_check_constraint(
        op.f("ck_payments_reversal_fields"),
        "payments",
        "(reversed_at IS NULL) = (reversed_by IS NULL) "
        "AND (reversed_at IS NULL) = (reversal_reason IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_payments_reversed_is_void"),
        "payments",
        "reversed_at IS NULL OR voided_at IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_payments_bounced_is_reversed"),
        "payments",
        "NOT bounced OR reversed_at IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_payments_reversal_reason_length"),
        "payments",
        "reversal_reason IS NULL OR length(reversal_reason) BETWEEN 1 AND 500",
    )

    op.add_column(
        "organizations",
        sa.Column(
            "payment_reminders_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "payment_reminder_days_after_due",
            sa.SmallInteger(),
            server_default=sa.text("7"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_organizations_payment_reminder_days_after_due"),
        "organizations",
        "payment_reminder_days_after_due BETWEEN 1 AND 365",
    )

    op.create_table(
        "payment_reminders",
        sa.Column("invoice_id", sa.UUID(), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("sent_on", sa.Date(), nullable=False),
        sa.Column("recipients", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("days_overdue", sa.Integer(), nullable=False),
        sa.Column("amount_due", sa.BigInteger(), nullable=False),
        sa.Column("sent_by", sa.UUID(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
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
            "source IN ('manual', 'scheduled')", name=op.f("ck_payment_reminders_source")
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'sent', 'failed', 'skipped')",
            name=op.f("ck_payment_reminders_status"),
        ),
        sa.CheckConstraint(
            "length(template) BETWEEN 1 AND 80", name=op.f("ck_payment_reminders_template_length")
        ),
        sa.CheckConstraint(
            "(source = 'scheduled') = (sent_by IS NULL)", name=op.f("ck_payment_reminders_sender")
        ),
        sa.CheckConstraint(
            "(status = 'sent') = (sent_at IS NOT NULL)", name=op.f("ck_payment_reminders_sent_at")
        ),
        sa.CheckConstraint(
            "cardinality(recipients) >= 1", name=op.f("ck_payment_reminders_has_recipients")
        ),
        sa.CheckConstraint(
            "days_overdue >= 0 AND amount_due >= 0", name=op.f("ck_payment_reminders_figures")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name=op.f("fk_payment_reminders_invoice"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_payment_reminders_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sent_by"], ["users.id"], name=op.f("fk_payment_reminders_sent_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_reminders")),
        sa.UniqueConstraint(
            "organization_id", "id", name=op.f("uq_payment_reminders_organization_id_id")
        ),
    )
    add_updated_at_triggers("payment_reminders")
    # One reminder per invoice and day: a failed or skipped attempt does not use the day up.
    op.create_index(
        "uq_payment_reminders_organization_id_invoice_id_sent_on",
        "payment_reminders",
        ["organization_id", "invoice_id", "sent_on"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'sent')"),
    )
    op.create_index(
        "ix_payment_reminders_organization_id_created_at",
        "payment_reminders",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_index("ix_payment_reminders_invoice_id", "payment_reminders", ["invoice_id"])
    op.create_index("ix_payment_reminders_batch_id", "payment_reminders", ["batch_id"])


def downgrade() -> None:
    op.drop_table("payment_reminders")

    op.drop_constraint(
        op.f("ck_organizations_payment_reminder_days_after_due"), "organizations", type_="check"
    )
    op.drop_column("organizations", "payment_reminder_days_after_due")
    op.drop_column("organizations", "payment_reminders_enabled")

    for name in (
        "ck_payments_reversal_reason_length",
        "ck_payments_bounced_is_reversed",
        "ck_payments_reversed_is_void",
        "ck_payments_reversal_fields",
    ):
        op.drop_constraint(op.f(name), "payments", type_="check")
    op.drop_constraint(op.f("fk_payments_reversed_by_users"), "payments", type_="foreignkey")
    # A reversed payment was void before the reversal existed.
    for column in ("bounced", "reversal_reason", "reversed_by", "reversed_at"):
        op.drop_column("payments", column)
