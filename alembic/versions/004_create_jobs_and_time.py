"""create jobs and time

Revision ID: 004
Revises: 003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGER_TABLES = (
    "jobs",
    "timesheets",
    "time_entries",
    "timers",
)


def upgrade() -> None:
    op.create_table(
        "timesheets",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
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
            "status IN ('open', 'submitted', 'approved', 'rejected')",
            name=op.f("ck_timesheets_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "decided_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_timesheets_organization_id_decided_by_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_timesheets_organization_id_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_timesheets_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_timesheets")),
        sa.UniqueConstraint("organization_id", "id", name="uq_timesheets_organization_id_id"),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "week_start",
            name="uq_timesheets_organization_id_user_id_week_start",
        ),
    )
    op.create_index(
        "ix_timesheets_organization_id_status_week_start",
        "timesheets",
        ["organization_id", "status", "week_start"],
        unique=False,
    )
    op.create_table(
        "jobs",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=True),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="planned", nullable=False),
        sa.Column("billing_basis", sa.Text(), server_default="hourly", nullable=False),
        sa.Column("fixed_fee", sa.BigInteger(), nullable=True),
        sa.Column("hourly_rate", sa.BigInteger(), nullable=True),
        sa.Column("budget_minutes", sa.Integer(), nullable=True),
        sa.Column("budget_fee", sa.BigInteger(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("partner_user_id", sa.UUID(), nullable=True),
        sa.Column("manager_user_id", sa.UUID(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "billing_basis <> 'fixed' OR fixed_fee IS NOT NULL",
            name=op.f("ck_jobs_fixed_fee_required"),
        ),
        sa.CheckConstraint(
            "billing_basis IN ('hourly', 'fixed', 'non_billable')",
            name=op.f("ck_jobs_billing_basis"),
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'in_progress', 'on_hold', 'completed', 'cancelled')",
            name=op.f("ck_jobs_status"),
        ),
        sa.CheckConstraint(
            "budget_fee IS NULL OR budget_fee >= 0", name=op.f("ck_jobs_budget_fee")
        ),
        sa.CheckConstraint(
            "budget_minutes IS NULL OR budget_minutes >= 0", name=op.f("ck_jobs_budget_minutes")
        ),
        sa.CheckConstraint("fixed_fee IS NULL OR fixed_fee >= 0", name=op.f("ck_jobs_fixed_fee")),
        sa.CheckConstraint(
            "hourly_rate IS NULL OR hourly_rate >= 0", name=op.f("ck_jobs_hourly_rate")
        ),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 200", name=op.f("ck_jobs_name_length")),
        sa.CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_start <= period_end",
            name=op.f("ck_jobs_period_order"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_jobs_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_jobs_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "manager_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_jobs_organization_id_manager_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "partner_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_jobs_organization_id_partner_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_jobs_organization_id_service_id_services",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_jobs_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint(
            "organization_id", "client_id", "id", name="uq_jobs_organization_id_client_id_id"
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_jobs_organization_id_id"),
    )
    op.create_index(
        "ix_jobs_organization_id_client_id", "jobs", ["organization_id", "client_id"], unique=False
    )
    op.create_index(
        "ix_jobs_organization_id_manager_user_id",
        "jobs",
        ["organization_id", "manager_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_jobs_organization_id_status_due_date",
        "jobs",
        ["organization_id", "status", "due_date"],
        unique=False,
    )
    op.create_index(
        "uq_jobs_organization_id_number", "jobs", ["organization_id", "number"], unique=True
    )
    op.create_table(
        "job_assignees",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            name="fk_job_assignees_organization_id_job_id_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_job_assignees_organization_id_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_job_assignees_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id", "user_id", name=op.f("pk_job_assignees")),
    )
    op.create_index(
        "ix_job_assignees_organization_id_user_id",
        "job_assignees",
        ["organization_id", "user_id"],
        unique=False,
    )
    op.create_table(
        "time_entries",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("timesheet_id", sa.UUID(), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("activity_code_id", sa.UUID(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_billable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("charge_rate", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_rate", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("charge_value", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_value", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
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
            name=op.f("ck_time_entries_billing_status"),
        ),
        sa.CheckConstraint(
            "is_billable OR billing_status = 'unbilled'",
            name=op.f("ck_time_entries_non_billable_status"),
        ),
        sa.CheckConstraint(
            "NOT is_billable OR client_id IS NOT NULL",
            name=op.f("ck_time_entries_billable_needs_client"),
        ),
        sa.CheckConstraint(
            "charge_rate >= 0 AND cost_rate >= 0 AND charge_value >= 0 AND cost_value >= 0",
            name=op.f("ck_time_entries_amounts"),
        ),
        sa.CheckConstraint(
            "job_id IS NULL OR client_id IS NOT NULL", name=op.f("ck_time_entries_job_needs_client")
        ),
        sa.CheckConstraint("minutes BETWEEN 1 AND 1440", name=op.f("ck_time_entries_minutes")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_time_entries_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "activity_code_id"],
            ["activity_codes.organization_id", "activity_codes.id"],
            name="fk_time_entries_organization_id_activity_code_id_activity_codes",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_time_entries_organization_id_client_id_job_id_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_time_entries_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "timesheet_id"],
            ["timesheets.organization_id", "timesheets.id"],
            name="fk_time_entries_organization_id_timesheet_id_timesheets",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_time_entries_organization_id_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_time_entries_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_time_entries")),
        sa.UniqueConstraint("organization_id", "id", name="uq_time_entries_organization_id_id"),
    )
    op.create_index(
        "ix_time_entries_organization_id_client_id_entry_date",
        "time_entries",
        ["organization_id", "client_id", "entry_date"],
        unique=False,
    )
    op.create_index(
        "ix_time_entries_organization_id_job_id",
        "time_entries",
        ["organization_id", "job_id"],
        unique=False,
    )
    op.create_index(
        "ix_time_entries_organization_id_user_id_entry_date",
        "time_entries",
        ["organization_id", "user_id", "entry_date"],
        unique=False,
    )
    op.create_index("ix_time_entries_timesheet_id", "time_entries", ["timesheet_id"], unique=False)
    op.create_index(
        "ix_time_entries_wip",
        "time_entries",
        ["organization_id", "client_id", "entry_date"],
        unique=False,
        postgresql_where=sa.text("is_billable AND billing_status = 'unbilled'"),
    )
    op.create_table(
        "timers",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("activity_code_id", sa.UUID(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_billable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accumulated_seconds", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.CheckConstraint("accumulated_seconds >= 0", name=op.f("ck_timers_accumulated_seconds")),
        sa.CheckConstraint(
            "job_id IS NULL OR client_id IS NOT NULL", name=op.f("ck_timers_job_needs_client")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "activity_code_id"],
            ["activity_codes.organization_id", "activity_codes.id"],
            name="fk_timers_organization_id_activity_code_id_activity_codes",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_timers_organization_id_client_id_job_id_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_timers_organization_id_client_id_clients",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_timers_organization_id_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_timers_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_timers")),
        sa.UniqueConstraint("organization_id", "user_id", name="uq_timers_organization_id_user_id"),
    )
    add_updated_at_triggers(*TRIGGER_TABLES)


def downgrade() -> None:
    op.drop_table("timers")
    op.drop_index(
        "ix_time_entries_wip",
        table_name="time_entries",
        postgresql_where=sa.text("is_billable AND billing_status = 'unbilled'"),
    )
    op.drop_index("ix_time_entries_timesheet_id", table_name="time_entries")
    op.drop_index("ix_time_entries_organization_id_user_id_entry_date", table_name="time_entries")
    op.drop_index("ix_time_entries_organization_id_job_id", table_name="time_entries")
    op.drop_index("ix_time_entries_organization_id_client_id_entry_date", table_name="time_entries")
    op.drop_table("time_entries")
    op.drop_index("ix_job_assignees_organization_id_user_id", table_name="job_assignees")
    op.drop_table("job_assignees")
    op.drop_index("uq_jobs_organization_id_number", table_name="jobs")
    op.drop_index("ix_jobs_organization_id_status_due_date", table_name="jobs")
    op.drop_index("ix_jobs_organization_id_manager_user_id", table_name="jobs")
    op.drop_index("ix_jobs_organization_id_client_id", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_timesheets_organization_id_status_week_start", table_name="timesheets")
    op.drop_table("timesheets")
