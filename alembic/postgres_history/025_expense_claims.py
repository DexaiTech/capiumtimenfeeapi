"""expense claims: claims, their lines and the history of decisions

Adds ``expense_claims`` (one person's claim, with its status and decision), ``expenses`` (the
lines: VAT, gross, miles, billable, recharge amount, payer and billing status, with the rate and
markup snapshots) and ``expense_claim_events`` (every submission, recall, approval, return and
correction). The partial index ``ix_expenses_wip`` serves the work-in-progress query for approved,
billable, unbilled lines.

Revision ID: 025
Revises: 024
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "025"
down_revision: str | None = "024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLAIM_STATUSES = "'draft', 'submitted', 'approved', 'returned'"
PAYERS = "'staff', 'practice'"
BILLING_STATUSES = "'unbilled', 'in_draft', 'billed', 'written_off'"
EVENT_ACTIONS = "'submitted', 'recalled', 'approved', 'returned', 'corrected'"


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def _organization_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="CASCADE",
    )


def _member_fk(column: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id", column],
        ["organization_members.organization_id", "organization_members.user_id"],
        name=name,
    )


def upgrade() -> None:
    op.create_table(
        "expense_claims",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(f"status IN ({CLAIM_STATUSES})", name=op.f("ck_expense_claims_status")),
        sa.CheckConstraint(
            "title IS NULL OR length(title) BETWEEN 1 AND 200",
            name=op.f("ck_expense_claims_title_length"),
        ),
        sa.CheckConstraint(
            "decision_note IS NULL OR length(decision_note) BETWEEN 1 AND 2000",
            name=op.f("ck_expense_claims_decision_note_length"),
        ),
        sa.CheckConstraint(
            "(status = 'draft' AND submitted_at IS NULL AND decided_at IS NULL "
            "AND decided_by IS NULL) "
            "OR (status = 'submitted' AND submitted_at IS NOT NULL AND decided_at IS NULL "
            "AND decided_by IS NULL) "
            "OR (status IN ('approved', 'returned') AND submitted_at IS NOT NULL "
            "AND decided_at IS NOT NULL AND decided_by IS NOT NULL)",
            name=op.f("ck_expense_claims_status_fields"),
        ),
        sa.CheckConstraint(
            "status <> 'returned' OR decision_note IS NOT NULL",
            name=op.f("ck_expense_claims_return_note"),
        ),
        _member_fk("user_id", "fk_expense_claims_organization_id_user_members"),
        _member_fk("created_by", "fk_expense_claims_organization_id_created_by_members"),
        _member_fk("decided_by", "fk_expense_claims_organization_id_decided_by_members"),
        _organization_fk("expense_claims"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expense_claims")),
        sa.UniqueConstraint("organization_id", "id", name="uq_expense_claims_organization_id_id"),
    )
    op.create_index(
        "ix_expense_claims_organization_id_user_id_created_at",
        "expense_claims",
        ["organization_id", "user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_expense_claims_organization_id_created_at",
        "expense_claims",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_expense_claims_submitted",
        "expense_claims",
        ["organization_id", "submitted_at"],
        unique=False,
        postgresql_where=sa.text("status = 'submitted'"),
    )

    op.create_table(
        "expenses",
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("expense_type_id", sa.UUID(), nullable=False),
        sa.Column("incurred_on", sa.Date(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("supplier", sa.Text(), nullable=True),
        sa.Column("vat_rate_id", sa.UUID(), nullable=True),
        sa.Column("vat_rate_bp", sa.Integer(), nullable=True),
        sa.Column("net", sa.BigInteger(), nullable=False),
        sa.Column("vat", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("gross", sa.BigInteger(), nullable=False),
        sa.Column("miles", sa.Numeric(8, 2), nullable=True),
        sa.Column("mileage_rate_applied", sa.BigInteger(), nullable=True),
        sa.Column("is_billable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("markup_bp", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("recharge_amount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("payer", sa.Text(), server_default="staff", nullable=False),
        sa.Column("billing_status", sa.Text(), server_default="unbilled", nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "length(description) BETWEEN 1 AND 500", name=op.f("ck_expenses_description_length")
        ),
        sa.CheckConstraint(
            "supplier IS NULL OR length(supplier) BETWEEN 1 AND 200",
            name=op.f("ck_expenses_supplier_length"),
        ),
        sa.CheckConstraint("net >= 0 AND vat >= 0", name=op.f("ck_expenses_amounts")),
        sa.CheckConstraint("gross = net + vat", name=op.f("ck_expenses_gross_total")),
        sa.CheckConstraint(
            "vat_rate_bp IS NULL OR vat_rate_bp BETWEEN 0 AND 10000",
            name=op.f("ck_expenses_vat_rate_bp"),
        ),
        sa.CheckConstraint(
            "(vat_rate_id IS NULL) = (vat_rate_bp IS NULL)", name=op.f("ck_expenses_vat_snapshot")
        ),
        sa.CheckConstraint("miles IS NULL OR miles > 0", name=op.f("ck_expenses_miles")),
        sa.CheckConstraint(
            "(miles IS NULL) = (mileage_rate_applied IS NULL) "
            "AND (mileage_rate_applied IS NULL OR mileage_rate_applied >= 0)",
            name=op.f("ck_expenses_mileage_snapshot"),
        ),
        sa.CheckConstraint("markup_bp BETWEEN 0 AND 100000", name=op.f("ck_expenses_markup_bp")),
        sa.CheckConstraint("recharge_amount >= 0", name=op.f("ck_expenses_recharge_amount")),
        sa.CheckConstraint(
            "is_billable OR recharge_amount = 0", name=op.f("ck_expenses_recharge_needs_billable")
        ),
        sa.CheckConstraint(
            "NOT is_billable OR client_id IS NOT NULL",
            name=op.f("ck_expenses_billable_needs_client"),
        ),
        sa.CheckConstraint(
            "job_id IS NULL OR client_id IS NOT NULL", name=op.f("ck_expenses_job_needs_client")
        ),
        sa.CheckConstraint(f"payer IN ({PAYERS})", name=op.f("ck_expenses_payer")),
        sa.CheckConstraint(
            f"billing_status IN ({BILLING_STATUSES})", name=op.f("ck_expenses_billing_status")
        ),
        sa.CheckConstraint(
            "is_billable OR billing_status = 'unbilled'",
            name=op.f("ck_expenses_non_billable_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "claim_id"],
            ["expense_claims.organization_id", "expense_claims.id"],
            name="fk_expenses_organization_id_claim_id_expense_claims",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "expense_type_id"],
            ["expense_types.organization_id", "expense_types.id"],
            name="fk_expenses_organization_id_expense_type_id_expense_types",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_expenses_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_expenses_organization_id_client_id_job_id_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_expenses_organization_id_vat_rate_id_vat_rates",
        ),
        _organization_fk("expenses"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expenses")),
        sa.UniqueConstraint("organization_id", "id", name="uq_expenses_organization_id_id"),
    )
    op.create_index(
        "ix_expenses_organization_id_claim_id", "expenses", ["organization_id", "claim_id"]
    )
    op.create_index(
        "ix_expenses_organization_id_client_id_incurred_on",
        "expenses",
        ["organization_id", "client_id", "incurred_on"],
        unique=False,
    )
    op.create_index(
        "ix_expenses_organization_id_incurred_on",
        "expenses",
        ["organization_id", "incurred_on", "id"],
        unique=False,
    )
    op.create_index(
        "ix_expenses_organization_id_job_id",
        "expenses",
        ["organization_id", "job_id"],
        unique=False,
        postgresql_where=sa.text("job_id IS NOT NULL"),
    )
    op.create_index(
        "ix_expenses_organization_id_expense_type_id",
        "expenses",
        ["organization_id", "expense_type_id"],
        unique=False,
    )
    op.create_index(
        "ix_expenses_wip",
        "expenses",
        ["organization_id", "client_id", "incurred_on"],
        unique=False,
        postgresql_where=sa.text(
            "is_billable AND billing_status = 'unbilled' AND recharge_amount > 0"
        ),
    )

    op.create_table(
        "expense_claim_events",
        sa.Column("claim_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            f"action IN ({EVENT_ACTIONS})", name=op.f("ck_expense_claim_events_action")
        ),
        sa.CheckConstraint(
            "note IS NULL OR length(note) BETWEEN 1 AND 2000",
            name=op.f("ck_expense_claim_events_note_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "claim_id"],
            ["expense_claims.organization_id", "expense_claims.id"],
            name="fk_expense_claim_events_claim_id_expense_claims",
            ondelete="CASCADE",
        ),
        _member_fk("actor_user_id", "fk_expense_claim_events_organization_id_actor_members"),
        _organization_fk("expense_claim_events"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expense_claim_events")),
    )
    op.create_index(
        "ix_expense_claim_events_organization_id_claim_id_at",
        "expense_claim_events",
        ["organization_id", "claim_id", "created_at"],
        unique=False,
    )

    add_updated_at_triggers("expense_claims", "expenses")


def downgrade() -> None:
    op.drop_table("expense_claim_events")
    op.drop_table("expenses")
    op.drop_table("expense_claims")
