"""estimates and recurring invoice schedules

Revision ID: 046
Revises: 045

Adds ``estimates`` and ``estimate_lines`` (quotes, numbered from the ``estimate`` sequence when
sent) and ``recurring_schedules``, ``recurring_lines`` and ``recurring_runs`` (templates that
raise a draft invoice on a schedule, one run row per schedule and date).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "046"
down_revision: str | None = "045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "estimates",
    "estimate_lines",
    "recurring_schedules",
    "recurring_lines",
    "recurring_runs",
)


def _base() -> list[sa.Column]:
    return [
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
    ]


def _org_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="CASCADE",
    )


def _client_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id", "client_id"],
        ["clients.organization_id", "clients.id"],
        name=f"fk_{table}_organization_id_client_id_clients",
    )


def _vat_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id", "vat_rate_id"],
        ["vat_rates.organization_id", "vat_rates.id"],
        name=f"fk_{table}_organization_id_vat_rate_id_vat_rates",
    )


def _job_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id", "job_id"],
        ["jobs.organization_id", "jobs.id"],
        name=f"fk_{table}_organization_id_job_id_jobs",
    )


def _service_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id", "service_id"],
        ["services.organization_id", "services.id"],
        name=f"fk_{table}_organization_id_service_id_services",
    )


def _user_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["created_by"], ["users.id"], name=op.f(f"fk_{table}_created_by_users")
    )


def _pk(table: str) -> sa.PrimaryKeyConstraint:
    return sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{table}"))


def _unique_id(table: str) -> sa.UniqueConstraint:
    return sa.UniqueConstraint("organization_id", "id", name=f"uq_{table}_organization_id_id")


def _ck(table: str, name: str, expr: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(expr, name=op.f(f"ck_{table}_{name}"))


def upgrade() -> None:
    op.create_table(
        "estimates",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("number", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("net_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("vat_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("gross_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("converted_invoice_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        *_base(),
        _ck("estimates", "status", "status IN ('draft', 'sent', 'accepted', 'declined')"),
        _ck("estimates", "currency_format", "currency ~ '^[A-Z]{3}$'"),
        _ck("estimates", "number_when_sent", "(status = 'draft') = (number IS NULL)"),
        _ck(
            "estimates",
            "expiry_after_issue",
            "expiry_date IS NULL OR issue_date IS NULL OR expiry_date >= issue_date",
        ),
        _ck("estimates", "gross_total", "gross_total = net_total + vat_total"),
        _client_fk("estimates"),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_estimates_organization_id_client_id_job_id_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "converted_invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name="fk_estimates_organization_id_converted_invoice_id_invoices",
        ),
        _org_fk("estimates"),
        _user_fk("estimates"),
        _pk("estimates"),
        _unique_id("estimates"),
    )
    op.create_index(
        "uq_estimates_organization_id_number",
        "estimates",
        ["organization_id", "number"],
        unique=True,
        postgresql_where=sa.text("number IS NOT NULL"),
    )
    op.create_index(
        "ix_estimates_organization_id_client_id", "estimates", ["organization_id", "client_id"]
    )
    op.create_index(
        "ix_estimates_organization_id_status", "estimates", ["organization_id", "status"]
    )

    op.create_table(
        "estimate_lines",
        sa.Column("estimate_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("service_id", sa.UUID(), nullable=True),
        sa.Column("quantity", sa.Numeric(12, 2), server_default=sa.text("1"), nullable=False),
        sa.Column("unit_amount", sa.BigInteger(), nullable=False),
        sa.Column("net_amount", sa.BigInteger(), nullable=False),
        sa.Column("vat_rate_id", sa.UUID(), nullable=True),
        sa.Column("vat_rate_bp", sa.Integer(), nullable=False),
        sa.Column("vat_amount", sa.BigInteger(), nullable=False),
        *_base(),
        _ck("estimate_lines", "quantity", "quantity > 0"),
        _ck("estimate_lines", "vat_rate_bp", "vat_rate_bp BETWEEN 0 AND 10000"),
        _ck(
            "estimate_lines",
            "description_length",
            "length(description) BETWEEN 1 AND 1000",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "estimate_id"],
            ["estimates.organization_id", "estimates.id"],
            name="fk_estimate_lines_organization_id_estimate_id_estimates",
            ondelete="CASCADE",
        ),
        _job_fk("estimate_lines"),
        _service_fk("estimate_lines"),
        _vat_fk("estimate_lines"),
        _org_fk("estimate_lines"),
        _pk("estimate_lines"),
    )
    op.create_index(
        "ix_estimate_lines_estimate_id_position", "estimate_lines", ["estimate_id", "position"]
    )

    op.create_table(
        "recurring_schedules",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("frequency", sa.Text(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("next_run_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("last_run_on", sa.Date(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        *_base(),
        _ck(
            "recurring_schedules",
            "frequency",
            "frequency IN ('weekly', 'monthly', 'quarterly', 'annually')",
        ),
        _ck("recurring_schedules", "status", "status IN ('active', 'paused', 'ended')"),
        _ck("recurring_schedules", "name_length", "length(name) BETWEEN 1 AND 200"),
        _ck(
            "recurring_schedules",
            "end_after_start",
            "end_date IS NULL OR end_date >= start_date",
        ),
        _ck("recurring_schedules", "next_after_start", "next_run_date >= start_date"),
        _client_fk("recurring_schedules"),
        _org_fk("recurring_schedules"),
        _user_fk("recurring_schedules"),
        _pk("recurring_schedules"),
        _unique_id("recurring_schedules"),
    )
    op.create_index(
        "ix_recurring_schedules_organization_id_client_id",
        "recurring_schedules",
        ["organization_id", "client_id"],
    )
    op.create_index(
        "ix_recurring_schedules_due",
        "recurring_schedules",
        ["next_run_date"],
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "recurring_lines",
        sa.Column("schedule_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("service_id", sa.UUID(), nullable=True),
        sa.Column("quantity", sa.Numeric(12, 2), server_default=sa.text("1"), nullable=False),
        sa.Column("unit_amount", sa.BigInteger(), nullable=False),
        sa.Column("vat_rate_id", sa.UUID(), nullable=True),
        *_base(),
        _ck("recurring_lines", "quantity", "quantity > 0"),
        _ck(
            "recurring_lines",
            "description_length",
            "length(description) BETWEEN 1 AND 1000",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["recurring_schedules.organization_id", "recurring_schedules.id"],
            name="fk_recurring_lines_organization_id_schedule_id_recurring_schedules",
            ondelete="CASCADE",
        ),
        _job_fk("recurring_lines"),
        _service_fk("recurring_lines"),
        _vat_fk("recurring_lines"),
        _org_fk("recurring_lines"),
        _pk("recurring_lines"),
    )
    op.create_index(
        "ix_recurring_lines_schedule_id_position", "recurring_lines", ["schedule_id", "position"]
    )

    op.create_table(
        "recurring_runs",
        sa.Column("schedule_id", sa.UUID(), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("invoice_id", sa.UUID(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "ran_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        *_base(),
        _ck("recurring_runs", "status", "status IN ('succeeded', 'failed')"),
        _ck(
            "recurring_runs",
            "invoice_when_succeeded",
            "(status = 'succeeded') = (invoice_id IS NOT NULL)",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["recurring_schedules.organization_id", "recurring_schedules.id"],
            name="fk_recurring_runs_organization_id_schedule_id_recurring_schedules",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name="fk_recurring_runs_organization_id_invoice_id_invoices",
        ),
        _org_fk("recurring_runs"),
        _pk("recurring_runs"),
        sa.UniqueConstraint(
            "schedule_id", "run_date", name="uq_recurring_runs_schedule_id_run_date"
        ),
    )
    op.create_index(
        "ix_recurring_runs_schedule_id_run_date", "recurring_runs", ["schedule_id", "run_date"]
    )

    add_updated_at_triggers(*TABLES)


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
