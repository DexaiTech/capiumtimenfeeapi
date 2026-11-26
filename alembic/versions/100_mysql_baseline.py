"""mysql baseline

Revision ID: 100
Revises:

The whole schema on MySQL 8.4, equivalent to PostgreSQL migration 082
(``alembic/postgres_history/``). Deliberate differences, all from the move to MySQL
(``docs/implementation-plans/AZURE-MYSQL.md``):

* ids are ``CHAR(36)`` (ASCII) UUID text made by the application; timestamps are
  ``DATETIME(6)`` in UTC; ``updated_at`` is kept by ``ON UPDATE CURRENT_TIMESTAMP(6)`` instead
  of a trigger;
* ``JSONB`` and arrays are ``JSON``; IP addresses are ``VARCHAR(45)``; text that is indexed,
  defaulted or bounded by a CHECK is ``VARCHAR(n)``, the rest ``TEXT``;
* partial unique indexes are functional unique indexes on ``CASE WHEN <condition> THEN <column>
  END`` (rows outside the condition are NULL and never collide); partial plain indexes are
  plain indexes;
* the three exclusion constraints (overlapping periods) and the append-only guards are
  triggers (below; ``app/db/triggers.py`` holds the same definitions);
* ``audit_logs`` is not partitioned; ``client_ref`` / ``ticket_ref`` are generated columns
  that index the client and ticket its metadata names;
* ``advisory_locks`` holds the rows behind the named locks (``app/db/locks.py``).

Downgrade drops every table (and so every trigger).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

from app.db.migration_helpers import sync_permissions

revision: str = "100"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# In creation order.
_TABLES = (
    "advisory_locks",
    "audit_logs",
    "background_jobs",
    "permissions",
    "rate_limit_counters",
    "users",
    "email_verification_tokens",
    "organizations",
    "password_reset_tokens",
    "user_sessions",
    "branches",
    "central_client_cache",
    "data_import_batches",
    "document_sequences",
    "email_templates",
    "grades",
    "integration_connections",
    "integration_cursors",
    "integration_sync_items",
    "integration_sync_logs",
    "invoice_templates",
    "locked_periods",
    "message_log",
    "notification_settings",
    "onboarding_progress",
    "period_locks",
    "rate_changes",
    "rate_reruns",
    "refresh_tokens",
    "roles",
    "segments",
    "ticket_tags",
    "time_entry_history",
    "timesheet_reminders",
    "vat_rates",
    "wip_movements",
    "wip_snapshots",
    "activity_codes",
    "bookkeeping_vat_mappings",
    "data_import_rows",
    "expense_types",
    "organization_members",
    "rate_rerun_items",
    "role_permissions",
    "services",
    "wip_snapshot_items",
    "bookkeeping_service_mappings",
    "clients",
    "expense_claims",
    "member_permission_overrides",
    "notification_preferences",
    "notifications",
    "overtime_rates",
    "period_lock_events",
    "service_activity_codes",
    "service_task_templates",
    "staff_hours_bases",
    "staff_segments",
    "tickets",
    "timesheets",
    "client_contacts",
    "client_services",
    "expense_claim_events",
    "invoices",
    "jobs",
    "leads",
    "payments",
    "rate_rules",
    "recurring_schedules",
    "statement_sends",
    "ticket_comments",
    "ticket_status_history",
    "ticket_tag_links",
    "credit_notes",
    "disbursements",
    "engagement_job_runs",
    "estimates",
    "expenses",
    "invoice_approvals",
    "invoice_lines",
    "job_assignees",
    "job_budget_alerts",
    "payment_allocations",
    "payment_reminders",
    "recurring_lines",
    "recurring_runs",
    "tasks",
    "ticket_attachments",
    "wip_adjustments",
    "wip_transfers",
    "wip_write_offs",
    "credit_note_lines",
    "credit_note_wip",
    "estimate_lines",
    "expense_receipts",
    "task_assignees",
    "time_entries",
    "timers",
    "time_entry_overtime",
    "wip_allocations",
)

_TRIGGERS = (
    "CREATE TRIGGER `trg_audit_logs_append_only_update` BEFORE UPDATE ON `audit_logs` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_logs is append-only'",
    "CREATE TRIGGER `trg_audit_logs_append_only_delete` BEFORE DELETE ON `audit_logs` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_logs is append-only'",
    "CREATE TRIGGER `ex_overtime_rates_scope_period_insert` BEFORE INSERT ON `overtime_rates` FOR EACH ROW BEGIN IF EXISTS (SELECT 1 FROM `overtime_rates` o WHERE o.`organization_id` <=> NEW.`organization_id` AND o.`user_id` <=> NEW.`user_id` AND o.`grade_id` <=> NEW.`grade_id` AND o.effective_from <= COALESCE(NEW.effective_to, '9999-12-31') AND NEW.effective_from <= COALESCE(o.effective_to, '9999-12-31')) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'ex_overtime_rates_scope_period'; END IF; END",
    "CREATE TRIGGER `ex_overtime_rates_scope_period_update` BEFORE UPDATE ON `overtime_rates` FOR EACH ROW BEGIN IF EXISTS (SELECT 1 FROM `overtime_rates` o WHERE o.`organization_id` <=> NEW.`organization_id` AND o.`user_id` <=> NEW.`user_id` AND o.`grade_id` <=> NEW.`grade_id` AND o.id <> NEW.id AND o.effective_from <= COALESCE(NEW.effective_to, '9999-12-31') AND NEW.effective_from <= COALESCE(o.effective_to, '9999-12-31')) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'ex_overtime_rates_scope_period'; END IF; END",
    "CREATE TRIGGER `trg_period_lock_events_append_only_update` BEFORE UPDATE ON `period_lock_events` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'period_lock_events is append-only'",
    "CREATE TRIGGER `trg_period_lock_events_append_only_delete` BEFORE DELETE ON `period_lock_events` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'period_lock_events is append-only'",
    "CREATE TRIGGER `ex_rate_rules_scope_period_insert` BEFORE INSERT ON `rate_rules` FOR EACH ROW BEGIN IF EXISTS (SELECT 1 FROM `rate_rules` o WHERE o.`organization_id` <=> NEW.`organization_id` AND o.`kind` <=> NEW.`kind` AND o.`client_id` <=> NEW.`client_id` AND o.`service_id` <=> NEW.`service_id` AND o.`user_id` <=> NEW.`user_id` AND o.`grade_id` <=> NEW.`grade_id` AND o.effective_from <= COALESCE(NEW.effective_to, '9999-12-31') AND NEW.effective_from <= COALESCE(o.effective_to, '9999-12-31')) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'ex_rate_rules_scope_period'; END IF; END",
    "CREATE TRIGGER `ex_rate_rules_scope_period_update` BEFORE UPDATE ON `rate_rules` FOR EACH ROW BEGIN IF EXISTS (SELECT 1 FROM `rate_rules` o WHERE o.`organization_id` <=> NEW.`organization_id` AND o.`kind` <=> NEW.`kind` AND o.`client_id` <=> NEW.`client_id` AND o.`service_id` <=> NEW.`service_id` AND o.`user_id` <=> NEW.`user_id` AND o.`grade_id` <=> NEW.`grade_id` AND o.id <> NEW.id AND o.effective_from <= COALESCE(NEW.effective_to, '9999-12-31') AND NEW.effective_from <= COALESCE(o.effective_to, '9999-12-31')) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'ex_rate_rules_scope_period'; END IF; END",
    "CREATE TRIGGER `ex_staff_hours_bases_user_period_insert` BEFORE INSERT ON `staff_hours_bases` FOR EACH ROW BEGIN IF EXISTS (SELECT 1 FROM `staff_hours_bases` o WHERE o.`organization_id` <=> NEW.`organization_id` AND o.`user_id` <=> NEW.`user_id` AND o.effective_from <= COALESCE(NEW.effective_to, '9999-12-31') AND NEW.effective_from <= COALESCE(o.effective_to, '9999-12-31')) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'ex_staff_hours_bases_user_period'; END IF; END",
    "CREATE TRIGGER `ex_staff_hours_bases_user_period_update` BEFORE UPDATE ON `staff_hours_bases` FOR EACH ROW BEGIN IF EXISTS (SELECT 1 FROM `staff_hours_bases` o WHERE o.`organization_id` <=> NEW.`organization_id` AND o.`user_id` <=> NEW.`user_id` AND o.id <> NEW.id AND o.effective_from <= COALESCE(NEW.effective_to, '9999-12-31') AND NEW.effective_from <= COALESCE(o.effective_to, '9999-12-31')) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'ex_staff_hours_bases_user_period'; END IF; END",
    "CREATE TRIGGER `trg_time_entry_history_append_only_update` BEFORE UPDATE ON `time_entry_history` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'time_entry_history is append-only'",
    "CREATE TRIGGER `trg_time_entry_history_append_only_delete` BEFORE DELETE ON `time_entry_history` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'time_entry_history is append-only'",
    "CREATE TRIGGER `trg_wip_movements_append_only_update` BEFORE UPDATE ON `wip_movements` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'wip_movements is append-only'",
    "CREATE TRIGGER `trg_wip_movements_append_only_delete` BEFORE DELETE ON `wip_movements` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'wip_movements is append-only'",
    "CREATE TRIGGER `trg_wip_snapshot_items_append_only_update` BEFORE UPDATE ON `wip_snapshot_items` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'wip_snapshot_items is append-only'",
    "CREATE TRIGGER `trg_wip_snapshot_items_append_only_delete` BEFORE DELETE ON `wip_snapshot_items` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'wip_snapshot_items is append-only'",
    "CREATE TRIGGER `trg_wip_snapshots_append_only_update` BEFORE UPDATE ON `wip_snapshots` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'wip_snapshots is append-only'",
    "CREATE TRIGGER `trg_wip_snapshots_append_only_delete` BEFORE DELETE ON `wip_snapshots` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'wip_snapshots is append-only'",
)


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "advisory_locks",
        sa.Column("lock_key", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("lock_key", name=op.f("pk_advisory_locks")),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "organization_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "actor_user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.String(length=255), nullable=True),
        sa.Column(
            "target_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), server_default=sa.text("(JSON_OBJECT())"), nullable=False),
        sa.Column(
            "client_ref",
            sa.String(length=64),
            sa.Computed(
                "json_unquote(json_extract(`metadata`, '$.client_id'))",
            ),
            nullable=True,
        ),
        sa.Column(
            "ticket_ref",
            sa.String(length=64),
            sa.Computed(
                "json_unquote(json_extract(`metadata`, '$.ticket_id'))",
            ),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id", "created_at", name=op.f("pk_audit_logs")),
    )
    op.create_index(
        "ix_audit_logs_organization_id_actor_user_id_created_at",
        "audit_logs",
        ["organization_id", "actor_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_organization_id_client_ref_created_at",
        "audit_logs",
        ["organization_id", "client_ref", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_organization_id_client_target_created_at",
        "audit_logs",
        ["organization_id", "target_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_organization_id_created_at",
        "audit_logs",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_organization_id_target_created_at",
        "audit_logs",
        ["organization_id", "target_type", "target_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_organization_id_ticket_ref_created_at",
        "audit_logs",
        ["organization_id", "ticket_ref", "created_at"],
        unique=False,
    )
    op.create_table(
        "background_jobs",
        sa.Column(
            "organization_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), server_default=sa.text("(JSON_OBJECT())"), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("100"), nullable=False),
        sa.Column(
            "run_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("locked_until", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=255), nullable=True),
        sa.Column("finished_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'dead')",
            name=op.f("ck_background_jobs_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_background_jobs")),
    )
    op.create_index(
        "ix_background_jobs_queued", "background_jobs", ["priority", "run_at"], unique=False
    )
    op.create_index(
        "ix_background_jobs_running_locked_until", "background_jobs", ["locked_until"], unique=False
    )
    op.create_index(
        "uq_background_jobs_dedupe_key",
        "background_jobs",
        [
            sa.literal_column(
                "(CASE WHEN dedupe_key IS NOT NULL AND status IN ('queued', 'running') THEN `dedupe_key` END)"
            )
        ],
        unique=True,
    )
    op.create_table(
        "permissions",
        sa.Column("code", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permissions")),
        sa.UniqueConstraint("code", name="uq_permissions_code"),
    )
    op.create_table(
        "rate_limit_counters",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("window_start", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("key", "window_start", name=op.f("pk_rate_limit_counters")),
    )
    op.create_table(
        "users",
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("full_name", sa.String(length=120), nullable=False),
        sa.Column(
            "timezone", sa.String(length=255), server_default="Europe/London", nullable=False
        ),
        sa.Column("email_verified_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "must_change_password", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "password_changed_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("last_login_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("avatar_storage_key", sa.Text(), nullable=True),
        sa.Column("avatar_content_type", sa.Text(), nullable=True),
        sa.Column("avatar_updated_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.CheckConstraint(
            "(avatar_storage_key IS NULL) = (avatar_content_type IS NULL) AND (avatar_storage_key IS NULL) = (avatar_updated_at IS NULL)",
            name=op.f("ck_users_avatar_all_or_none"),
        ),
        sa.CheckConstraint(
            "char_length(full_name) BETWEEN 1 AND 120", name=op.f("ck_users_full_name_length")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(
        "uq_users_email_lower",
        "users",
        [sa.literal_column("(CASE WHEN deleted_at IS NULL THEN lower(email) END)")],
        unique=True,
    )
    op.create_table(
        "email_verification_tokens",
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("used_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("requested_ip", sa.String(length=45), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_email_verification_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_verification_tokens")),
    )
    op.create_index(
        "ix_email_verification_tokens_user_id",
        "email_verification_tokens",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "uq_email_verification_tokens_token_hash",
        "email_verification_tokens",
        ["token_hash"],
        unique=True,
    )
    op.create_table(
        "organizations",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column(
            "timezone", sa.String(length=255), server_default="Europe/London", nullable=False
        ),
        sa.Column("week_start", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "working_days",
            sa.JSON(),
            server_default=sa.text("(JSON_ARRAY(1, 2, 3, 4, 5))"),
            nullable=False,
        ),
        sa.Column("minutes_per_day", sa.Integer(), server_default=sa.text("450"), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="GBP", nullable=False),
        sa.Column("vat_registered", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("vat_number", sa.Text(), nullable=True),
        sa.Column("company_number", sa.Text(), nullable=True),
        sa.Column(
            "payment_terms_days", sa.SmallInteger(), server_default=sa.text("30"), nullable=False
        ),
        sa.Column(
            "default_charge_rate", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "time_increment_minutes", sa.SmallInteger(), server_default=sa.text("6"), nullable=False
        ),
        sa.Column(
            "require_timesheet_approval",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "auto_approve_privileged_time",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "fixed_fee_wip_mode",
            sa.String(length=32),
            server_default="on_completion",
            nullable=False,
        ),
        sa.Column(
            "wip_adjustment_approval_threshold",
            sa.BigInteger(),
            server_default=sa.text("50000"),
            nullable=False,
        ),
        sa.Column(
            "wip_write_off_approval_threshold_percent",
            sa.Integer(),
            server_default=sa.text("1000"),
            nullable=True,
        ),
        sa.Column(
            "wip_adjustment_approval_required",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "expense_approval_required",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "receipt_required_threshold",
            sa.BigInteger(),
            server_default=sa.text("2500"),
            nullable=False,
        ),
        sa.Column(
            "invoice_approval_required",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "invoice_approval_threshold",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "payment_reminders_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "payment_reminder_days_after_due",
            sa.SmallInteger(),
            server_default=sa.text("7"),
            nullable=False,
        ),
        sa.Column(
            "payment_reminder_schedule",
            sa.JSON(),
            server_default=sa.text("(JSON_ARRAY())"),
            nullable=False,
        ),
        sa.Column("mileage_rate", sa.BigInteger(), server_default=sa.text("45"), nullable=False),
        sa.Column("expense_markup_bp", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "wip_valuation_sources",
            sa.JSON(),
            server_default=sa.text("(JSON_ARRAY('client', 'service', 'grade', 'staff'))"),
            nullable=False,
        ),
        sa.Column("rate_mode", sa.String(length=32), server_default="staff_first", nullable=False),
        sa.Column(
            "period_lock_granularity", sa.String(length=32), server_default="weekly", nullable=False
        ),
        sa.Column(
            "financial_year_start_month",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "financial_year_start_day",
            sa.SmallInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "period_lock_configured", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "overtime_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "overtime_multiplier_bp", sa.Integer(), server_default=sa.text("15000"), nullable=False
        ),
        sa.Column(
            "cost_plus_markup_bp", sa.Integer(), server_default=sa.text("5000"), nullable=False
        ),
        sa.Column(
            "recovery_rate_order",
            sa.JSON(),
            server_default=sa.text("(JSON_ARRAY('client_job', 'task', 'staff'))"),
            nullable=False,
        ),
        sa.Column(
            "client_segments_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "branches_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("address_line1", sa.Text(), nullable=True),
        sa.Column("address_line2", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("postcode", sa.Text(), nullable=True),
        sa.Column("country", sa.String(length=255), server_default="GB", nullable=False),
        sa.Column("bank_details", sa.Text(), nullable=True),
        sa.Column("invoice_footer", sa.Text(), nullable=True),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.CheckConstraint(
            "JSON_TYPE(payment_reminder_schedule) = 'ARRAY' AND JSON_LENGTH(payment_reminder_schedule) <= 12",
            name=op.f("ck_organizations_payment_reminder_schedule"),
        ),
        sa.CheckConstraint(
            "JSON_TYPE(recovery_rate_order) = 'ARRAY' AND JSON_LENGTH(recovery_rate_order) = 3 AND JSON_CONTAINS(recovery_rate_order, JSON_ARRAY('client_job', 'task', 'staff'))",
            name=op.f("ck_organizations_recovery_rate_order"),
        ),
        sa.CheckConstraint(
            "JSON_TYPE(wip_valuation_sources) = 'ARRAY' AND JSON_LENGTH(wip_valuation_sources) BETWEEN 1 AND 4 AND JSON_CONTAINS(JSON_ARRAY('client', 'service', 'grade', 'staff'), wip_valuation_sources) AND COALESCE(JSON_LENGTH(JSON_SEARCH(wip_valuation_sources, 'all', 'client')), 0) <= 1 AND COALESCE(JSON_LENGTH(JSON_SEARCH(wip_valuation_sources, 'all', 'service')), 0) <= 1 AND COALESCE(JSON_LENGTH(JSON_SEARCH(wip_valuation_sources, 'all', 'grade')), 0) <= 1 AND COALESCE(JSON_LENGTH(JSON_SEARCH(wip_valuation_sources, 'all', 'staff')), 0) <= 1",
            name=op.f("ck_organizations_wip_valuation_sources"),
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(currency, '^[A-Z]{3}$', 'c')",
            name=op.f("ck_organizations_currency_format"),
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(slug, '^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$', 'c')",
            name=op.f("ck_organizations_slug_format"),
        ),
        sa.CheckConstraint(
            "fixed_fee_wip_mode IN ('on_completion', 'by_budget_progress')",
            name=op.f("ck_organizations_fixed_fee_wip_mode"),
        ),
        sa.CheckConstraint(
            "period_lock_granularity IN ('weekly', 'monthly', 'quarterly', 'annually')",
            name=op.f("ck_organizations_period_lock_granularity"),
        ),
        sa.CheckConstraint(
            "rate_mode IN ('staff_first', 'grade_only', 'staff_only')",
            name=op.f("ck_organizations_rate_mode"),
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 120", name=op.f("ck_organizations_name_length")
        ),
        sa.CheckConstraint(
            "cost_plus_markup_bp BETWEEN 0 AND 100000",
            name=op.f("ck_organizations_cost_plus_markup_bp"),
        ),
        sa.CheckConstraint(
            "default_charge_rate >= 0", name=op.f("ck_organizations_default_charge_rate")
        ),
        sa.CheckConstraint(
            "expense_markup_bp BETWEEN 0 AND 100000",
            name=op.f("ck_organizations_expense_markup_bp"),
        ),
        sa.CheckConstraint(
            "financial_year_start_month BETWEEN 1 AND 12 AND financial_year_start_day BETWEEN 1 AND 28",
            name=op.f("ck_organizations_financial_year_start"),
        ),
        sa.CheckConstraint(
            "invoice_approval_threshold >= 0",
            name=op.f("ck_organizations_invoice_approval_threshold"),
        ),
        sa.CheckConstraint("mileage_rate >= 0", name=op.f("ck_organizations_mileage_rate")),
        sa.CheckConstraint(
            "minutes_per_day BETWEEN 1 AND 1440", name=op.f("ck_organizations_minutes_per_day")
        ),
        sa.CheckConstraint(
            "overtime_multiplier_bp BETWEEN 10000 AND 100000",
            name=op.f("ck_organizations_overtime_multiplier_bp"),
        ),
        sa.CheckConstraint(
            "payment_reminder_days_after_due BETWEEN 1 AND 365",
            name=op.f("ck_organizations_payment_reminder_days_after_due"),
        ),
        sa.CheckConstraint(
            "payment_terms_days BETWEEN 0 AND 365", name=op.f("ck_organizations_payment_terms_days")
        ),
        sa.CheckConstraint(
            "receipt_required_threshold >= 0",
            name=op.f("ck_organizations_receipt_required_threshold"),
        ),
        sa.CheckConstraint(
            "time_increment_minutes IN (1, 5, 6, 10, 15, 30, 60)",
            name=op.f("ck_organizations_increment"),
        ),
        sa.CheckConstraint("week_start BETWEEN 1 AND 7", name=op.f("ck_organizations_week_start")),
        sa.CheckConstraint(
            "wip_adjustment_approval_threshold >= 0",
            name=op.f("ck_organizations_wip_adjustment_approval_threshold"),
        ),
        sa.CheckConstraint(
            "wip_write_off_approval_threshold_percent IS NULL OR wip_write_off_approval_threshold_percent BETWEEN 0 AND 10000",
            name=op.f("ck_organizations_wip_write_off_approval_threshold_percent"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_organizations_created_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
    )
    op.create_index(
        "uq_organizations_slug",
        "organizations",
        [sa.literal_column("(CASE WHEN deleted_at IS NULL THEN `slug` END)")],
        unique=True,
    )
    op.create_table(
        "password_reset_tokens",
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("used_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("requested_ip", sa.String(length=45), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_password_reset_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_password_reset_tokens")),
    )
    op.create_index(
        "ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"], unique=False
    )
    op.create_index(
        "uq_password_reset_tokens_token_hash", "password_reset_tokens", ["token_hash"], unique=True
    )
    op.create_table(
        "user_sessions",
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "last_used_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("revoked_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("revoked_reason", sa.String(length=32), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revoked_reason IS NULL OR revoked_reason IN ('logout', 'password_change', 'reuse_detected', 'admin', 'user')",
            name=op.f("ck_user_sessions_revoked_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_sessions")),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"], unique=False)
    op.create_table(
        "branches",
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 40", name=op.f("ck_branches_name_length")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_branches_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_branches")),
        sa.UniqueConstraint("organization_id", "id", name="uq_branches_organization_id_id"),
    )
    op.create_index(
        "uq_branches_organization_id_name_lower",
        "branches",
        ["organization_id", sa.literal_column("(lower(name))")],
        unique=True,
    )
    op.create_table(
        "central_client_cache",
        sa.Column("central_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("company_number", sa.String(length=255), nullable=True),
        sa.Column("address_line1", sa.Text(), nullable=True),
        sa.Column("address_line2", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("postcode", sa.Text(), nullable=True),
        sa.Column("country", sa.String(length=255), server_default="GB", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("source_updated_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column(
            "fetched_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived')", name=op.f("ck_central_client_cache_status")
        ),
        sa.CheckConstraint(
            "char_length(central_id) BETWEEN 1 AND 64",
            name=op.f("ck_central_client_cache_central_id_length"),
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 200", name=op.f("ck_central_client_cache_name_length")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_central_client_cache_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_central_client_cache")),
        sa.UniqueConstraint(
            "organization_id",
            "central_id",
            name="uq_central_client_cache_organization_id_central_id",
        ),
    )
    op.create_index(
        "ix_central_client_cache_organization_id_company_number",
        "central_client_cache",
        ["organization_id", "company_number"],
        unique=False,
    )
    op.create_index(
        "ix_central_client_cache_organization_id_name_lower",
        "central_client_cache",
        ["organization_id", sa.literal_column("(lower(name))")],
        unique=False,
    )
    op.create_table(
        "data_import_batches",
        sa.Column("entity", sa.String(length=32), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("file_format", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("options", sa.JSON(), server_default=sa.text("(JSON_OBJECT())"), nullable=False),
        sa.Column("mapping", sa.JSON(), server_default=sa.text("(JSON_OBJECT())"), nullable=False),
        sa.Column("columns", sa.JSON(), server_default=sa.text("(JSON_OBJECT())"), nullable=False),
        sa.Column("total_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("valid_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("warning_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rejected_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("unchanged_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "committed_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("committed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "entity IN ('staff', 'rates', 'jobs')", name=op.f("ck_data_import_batches_entity")
        ),
        sa.CheckConstraint(
            "file_format IN ('csv', 'xlsx')", name=op.f("ck_data_import_batches_file_format")
        ),
        sa.CheckConstraint(
            "status <> 'committed' OR committed_at IS NOT NULL",
            name=op.f("ck_data_import_batches_committed_at"),
        ),
        sa.CheckConstraint(
            "status IN ('validated', 'committed', 'failed')",
            name=op.f("ck_data_import_batches_status"),
        ),
        sa.CheckConstraint(
            "char_length(filename) BETWEEN 1 AND 255",
            name=op.f("ck_data_import_batches_filename_length"),
        ),
        sa.CheckConstraint(
            "total_rows >= 0 AND valid_rows >= 0 AND warning_rows >= 0 AND rejected_rows >= 0 AND created_rows >= 0 AND updated_rows >= 0 AND unchanged_rows >= 0",
            name=op.f("ck_data_import_batches_counts"),
        ),
        sa.ForeignKeyConstraint(
            ["committed_by"],
            ["users.id"],
            name=op.f("fk_data_import_batches_committed_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_data_import_batches_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_data_import_batches_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_import_batches")),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_data_import_batches_organization_id_id"
        ),
    )
    op.create_index(
        "ix_data_import_batches_organization_id_created_at",
        "data_import_batches",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "document_sequences",
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("prefix", sa.String(length=12), server_default="", nullable=False),
        sa.Column("suffix", sa.String(length=12), server_default="", nullable=False),
        sa.Column("next_number", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("padding", sa.SmallInteger(), server_default=sa.text("5"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(prefix, '^[A-Za-z0-9/_-]{0,12}$', 'c')",
            name=op.f("ck_document_sequences_prefix_format"),
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(suffix, '^[A-Za-z0-9/_-]{0,12}$', 'c')",
            name=op.f("ck_document_sequences_suffix_format"),
        ),
        sa.CheckConstraint(
            "kind IN ('client', 'job', 'invoice', 'credit_note', 'staff', 'ticket', 'estimate')",
            name=op.f("ck_document_sequences_kind"),
        ),
        sa.CheckConstraint("next_number >= 1", name=op.f("ck_document_sequences_next_number")),
        sa.CheckConstraint("padding BETWEEN 1 AND 10", name=op.f("ck_document_sequences_padding")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_document_sequences_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("organization_id", "kind", name=op.f("pk_document_sequences")),
    )
    op.create_table(
        "email_templates",
        sa.Column("template_key", sa.String(length=49), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=False),
        sa.Column(
            "updated_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(template_key, '^[a-z][a-z0-9_]{1,49}$', 'c')",
            name=op.f("ck_email_templates_key_format"),
        ),
        sa.CheckConstraint(
            "char_length(subject) BETWEEN 1 AND 200 AND NOT REGEXP_LIKE(subject, '[[:cntrl:]]', 'c')",
            name=op.f("ck_email_templates_subject_valid"),
        ),
        sa.CheckConstraint(
            "char_length(html_body) BETWEEN 1 AND 100000",
            name=op.f("ck_email_templates_html_body_length"),
        ),
        sa.CheckConstraint(
            "char_length(text_body) BETWEEN 1 AND 50000",
            name=op.f("ck_email_templates_text_body_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_email_templates_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], name=op.f("fk_email_templates_updated_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_templates")),
        sa.UniqueConstraint(
            "organization_id",
            "template_key",
            name="uq_email_templates_organization_id_template_key",
        ),
    )
    op.create_table(
        "grades",
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("default_cost_rate", sa.BigInteger(), nullable=True),
        sa.Column("default_charge_rate", sa.BigInteger(), nullable=True),
        sa.Column("default_chargeable_target_bp", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(code, '^[A-Z0-9_-]{1,12}$', 'c')", name=op.f("ck_grades_code_format")
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 80", name=op.f("ck_grades_name_length")
        ),
        sa.CheckConstraint(
            "default_charge_rate IS NULL OR default_charge_rate >= 0",
            name=op.f("ck_grades_default_charge_rate"),
        ),
        sa.CheckConstraint(
            "default_chargeable_target_bp IS NULL OR default_chargeable_target_bp BETWEEN 0 AND 10000",
            name=op.f("ck_grades_default_chargeable_target_bp"),
        ),
        sa.CheckConstraint(
            "default_cost_rate IS NULL OR default_cost_rate >= 0",
            name=op.f("ck_grades_default_cost_rate"),
        ),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) BETWEEN 1 AND 500",
            name=op.f("ck_grades_description_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_grades_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grades")),
        sa.UniqueConstraint("organization_id", "id", name="uq_grades_organization_id_id"),
    )
    op.create_index(
        "uq_grades_organization_id_code", "grades", ["organization_id", "code"], unique=True
    )
    op.create_index(
        "uq_grades_organization_id_name_lower",
        "grades",
        ["organization_id", sa.literal_column("(lower(name))")],
        unique=True,
    )
    op.create_table(
        "integration_connections",
        sa.Column("integration", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="idle", nullable=False),
        sa.Column("last_success_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_attempt_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("config", sa.JSON(), server_default=sa.text("(JSON_OBJECT())"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "integration IN ('central_client', 'central_user', 'practice_management', 'bookkeeping')",
            name=op.f("ck_integration_connections_integration"),
        ),
        sa.CheckConstraint(
            "status IN ('idle', 'ok', 'error')", name=op.f("ck_integration_connections_status")
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 1000",
            name=op.f("ck_integration_connections_last_error_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_integration_connections_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_connections")),
        sa.UniqueConstraint(
            "organization_id",
            "integration",
            name="uq_integration_connections_organization_id_integration",
        ),
    )
    op.create_table(
        "integration_cursors",
        sa.Column("integration", sa.String(length=39), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(integration, '^[a-z][a-z0-9_]{1,39}$', 'c')",
            name=op.f("ck_integration_cursors_integration_format"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_integration_cursors_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id", "integration", name=op.f("pk_integration_cursors")
        ),
    )
    op.create_table(
        "integration_sync_items",
        sa.Column("integration", sa.String(length=255), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column(
            "entity_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("direction", sa.String(length=32), server_default="outbound", nullable=False),
        sa.Column("operation", sa.String(length=60), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_attempt_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("completed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("external_ref", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), server_default=sa.text("(JSON_OBJECT())"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'pending') = (next_attempt_at IS NOT NULL)",
            name=op.f("ck_integration_sync_items_next_attempt"),
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')", name=op.f("ck_integration_sync_items_direction")
        ),
        sa.CheckConstraint(
            "integration IN ('central_client', 'central_user', 'practice_management', 'bookkeeping')",
            name=op.f("ck_integration_sync_items_integration"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ok', 'failed', 'skipped')",
            name=op.f("ck_integration_sync_items_status"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_integration_sync_items_attempts")),
        sa.CheckConstraint(
            "char_length(idempotency_key) BETWEEN 1 AND 200",
            name=op.f("ck_integration_sync_items_idempotency_key_length"),
        ),
        sa.CheckConstraint(
            "char_length(operation) BETWEEN 1 AND 60",
            name=op.f("ck_integration_sync_items_operation_length"),
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR char_length(last_error) <= 1000",
            name=op.f("ck_integration_sync_items_last_error_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_integration_sync_items_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_sync_items")),
        sa.UniqueConstraint(
            "organization_id",
            "integration",
            "idempotency_key",
            name="uq_integration_sync_items_organization_id_idempotency_key",
        ),
    )
    op.create_index(
        "ix_integration_sync_items_due", "integration_sync_items", ["next_attempt_at"], unique=False
    )
    op.create_index(
        "ix_integration_sync_items_org_integration_status",
        "integration_sync_items",
        ["organization_id", "integration", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_integration_sync_items_organization_id_entity_id",
        "integration_sync_items",
        ["organization_id", "entity_id"],
        unique=False,
    )
    op.create_table(
        "integration_sync_logs",
        sa.Column("integration", sa.String(length=39), nullable=False),
        sa.Column("direction", sa.String(length=32), server_default="inbound", nullable=False),
        sa.Column("operation", sa.String(length=60), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column(
            "entity_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("external_event_id", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("message", sa.String(length=1000), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(integration, '^[a-z][a-z0-9_]{1,39}$', 'c')",
            name=op.f("ck_integration_sync_logs_integration_format"),
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')", name=op.f("ck_integration_sync_logs_direction")
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'failed', 'skipped')", name=op.f("ck_integration_sync_logs_status")
        ),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_integration_sync_logs_attempt")),
        sa.CheckConstraint(
            "char_length(operation) BETWEEN 1 AND 60",
            name=op.f("ck_integration_sync_logs_operation_length"),
        ),
        sa.CheckConstraint(
            "message IS NULL OR char_length(message) <= 1000",
            name=op.f("ck_integration_sync_logs_message_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_integration_sync_logs_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_sync_logs")),
    )
    op.create_index(
        "ix_integration_sync_logs_organization_id_created_at",
        "integration_sync_logs",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_integration_sync_logs_organization_id_entity_id_created_at",
        "integration_sync_logs",
        ["organization_id", "entity_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_integration_sync_logs_event",
        "integration_sync_logs",
        [
            sa.literal_column(
                "(CASE WHEN external_event_id IS NOT NULL THEN `organization_id` END)"
            ),
            sa.literal_column("(CASE WHEN external_event_id IS NOT NULL THEN `integration` END)"),
            sa.literal_column(
                "(CASE WHEN external_event_id IS NOT NULL THEN `external_event_id` END)"
            ),
        ],
        unique=True,
    )
    op.create_table(
        "invoice_templates",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("layout", sa.String(length=32), server_default="standard", nullable=False),
        sa.Column("show_logo", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "show_bank_details", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("footer_text", sa.String(length=1000), nullable=True),
        sa.Column("accent_color", sa.String(length=6), nullable=True),
        sa.Column("watermark_text", sa.String(length=24), nullable=True),
        sa.Column("watermark_image_key", sa.Text(), nullable=True),
        sa.Column("watermark_image_type", sa.Text(), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.CheckConstraint(
            "NOT is_default OR status = 'active'",
            name=op.f("ck_invoice_templates_default_is_active"),
        ),
        sa.CheckConstraint(
            "accent_color IS NULL OR REGEXP_LIKE(accent_color, '^#[0-9A-Fa-f]{6}$', 'c')",
            name=op.f("ck_invoice_templates_accent_color_format"),
        ),
        sa.CheckConstraint(
            "layout IN ('standard', 'detailed', 'summary')",
            name=op.f("ck_invoice_templates_layout"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')", name=op.f("ck_invoice_templates_status")
        ),
        sa.CheckConstraint(
            "watermark_text IS NULL OR REGEXP_LIKE(watermark_text, '^[A-Za-z0-9 -]{1,24}$', 'c')",
            name=op.f("ck_invoice_templates_watermark_text_format"),
        ),
        sa.CheckConstraint(
            "(watermark_image_key IS NULL) = (watermark_image_type IS NULL)",
            name=op.f("ck_invoice_templates_watermark_image_all_or_none"),
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 80", name=op.f("ck_invoice_templates_name_length")
        ),
        sa.CheckConstraint(
            "footer_text IS NULL OR char_length(footer_text) BETWEEN 1 AND 1000",
            name=op.f("ck_invoice_templates_footer_text_length"),
        ),
        sa.CheckConstraint(
            "watermark_text IS NULL OR watermark_image_key IS NULL",
            name=op.f("ck_invoice_templates_watermark_text_or_image"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_invoice_templates_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_templates")),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_invoice_templates_organization_id_id"
        ),
    )
    op.create_index(
        "uq_invoice_templates_organization_id_default",
        "invoice_templates",
        [
            sa.literal_column(
                "(CASE WHEN is_default AND deleted_at IS NULL THEN `organization_id` END)"
            )
        ],
        unique=True,
    )
    op.create_index(
        "uq_invoice_templates_organization_id_name_lower",
        "invoice_templates",
        [
            sa.literal_column("(CASE WHEN deleted_at IS NULL THEN `organization_id` END)"),
            sa.literal_column("(CASE WHEN deleted_at IS NULL THEN lower(name) END)"),
        ],
        unique=True,
    )
    op.create_table(
        "locked_periods",
        sa.Column("period_type", sa.String(length=32), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "locked_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "locked_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "period_type IN ('week', 'month', 'quarter', 'year', 'legacy')",
            name=op.f("ck_locked_periods_period_type"),
        ),
        sa.CheckConstraint("start_date <= end_date", name=op.f("ck_locked_periods_range")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_locked_periods_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_locked_periods")),
        sa.UniqueConstraint(
            "organization_id",
            "period_type",
            "start_date",
            name="uq_locked_periods_organization_id_period_type_start_date",
        ),
    )
    op.create_index(
        "ix_locked_periods_organization_id_dates",
        "locked_periods",
        ["organization_id", "start_date", "end_date"],
        unique=False,
    )
    op.create_table(
        "message_log",
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "organization_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=True),
        sa.Column("template_key", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_ref", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.CheckConstraint("channel IN ('email', 'sms')", name=op.f("ck_message_log_channel")),
        sa.CheckConstraint("status IN ('sent', 'failed')", name=op.f("ck_message_log_status")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_message_log_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_log")),
    )
    op.create_index("ix_message_log_created_at", "message_log", ["created_at"], unique=False)
    op.create_index(
        "ix_message_log_organization_id_created_at",
        "message_log",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "notification_settings",
        sa.Column("type", sa.String(length=49), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("default_in_app", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("default_email", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("default_sms", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "updated_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(type, '^[a-z][a-z0-9_]{1,49}$', 'c')",
            name=op.f("ck_notification_settings_type_format"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_notification_settings_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], name=op.f("fk_notification_settings_updated_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_settings")),
        sa.UniqueConstraint(
            "organization_id", "type", name="uq_notification_settings_organization_id_type"
        ),
    )
    op.create_table(
        "onboarding_progress",
        sa.Column("step_key", sa.String(length=39), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column(
            "updated_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(step_key, '^[a-z][a-z_]{0,39}$', 'c')",
            name=op.f("ck_onboarding_progress_step_key_format"),
        ),
        sa.CheckConstraint(
            "state IN ('done', 'skipped', 'todo')", name=op.f("ck_onboarding_progress_state")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_onboarding_progress_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_onboarding_progress_updated_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_onboarding_progress")),
        sa.UniqueConstraint(
            "organization_id", "step_key", name="uq_onboarding_progress_organization_id_step_key"
        ),
    )
    op.create_table(
        "period_locks",
        sa.Column("locked_through", sa.Date(), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_period_locks_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_period_locks")),
        sa.UniqueConstraint("organization_id", "id", name="uq_period_locks_organization_id_id"),
        sa.UniqueConstraint("organization_id", name="uq_period_locks_organization_id"),
    )
    op.create_table(
        "rate_changes",
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("rule_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "service_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column(
            "grade_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("old", sa.JSON(), nullable=True),
        sa.Column("new", sa.JSON(), nullable=True),
        sa.Column(
            "actor_user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("source", sa.String(length=255), server_default="rates", nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('created', 'updated', 'end_dated', 'deleted')",
            name=op.f("ck_rate_changes_action"),
        ),
        sa.CheckConstraint("kind IN ('charge', 'cost')", name=op.f("ck_rate_changes_kind")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_rate_changes_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_changes")),
    )
    op.create_index(
        "ix_rate_changes_organization_id_client_id_created_at",
        "rate_changes",
        ["organization_id", "client_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_rate_changes_organization_id_created_at",
        "rate_changes",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_rate_changes_organization_id_grade_id_created_at",
        "rate_changes",
        ["organization_id", "grade_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_rate_changes_organization_id_user_id_created_at",
        "rate_changes",
        ["organization_id", "user_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_rate_changes_rule_id", "rate_changes", ["rule_id"], unique=False)
    op.create_table(
        "rate_reruns",
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.JSON(), nullable=False),
        sa.Column("scope_hash", sa.Text(), nullable=False),
        sa.Column("entries_examined", sa.Integer(), nullable=False),
        sa.Column("entries_changed", sa.Integer(), nullable=False),
        sa.Column("entries_skipped", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.JSON(), server_default=sa.text("(JSON_OBJECT())"), nullable=False),
        sa.Column("charge_value_before", sa.BigInteger(), nullable=False),
        sa.Column("charge_value_after", sa.BigInteger(), nullable=False),
        sa.Column("cost_value_before", sa.BigInteger(), nullable=False),
        sa.Column("cost_value_after", sa.BigInteger(), nullable=False),
        sa.Column(
            "requested_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint("source IN ('rerate', 'revalue')", name=op.f("ck_rate_reruns_source")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_rate_reruns_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name=op.f("fk_rate_reruns_requested_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_reruns")),
        sa.UniqueConstraint("organization_id", "id", name="uq_rate_reruns_organization_id_id"),
    )
    op.create_index(
        "ix_rate_reruns_organization_id_created_at",
        "rate_reruns",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "refresh_tokens",
        sa.Column(
            "session_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("used_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "replaced_by_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"],
            ["refresh_tokens.id"],
            name=op.f("fk_refresh_tokens_replaced_by_id_refresh_tokens"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["user_sessions.id"],
            name=op.f("fk_refresh_tokens_session_id_user_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_tokens")),
    )
    op.create_index("ix_refresh_tokens_session_id", "refresh_tokens", ["session_id"], unique=False)
    op.create_index("uq_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True)
    op.create_table(
        "roles",
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("system_key", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 60", name=op.f("ck_roles_name_length")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_roles_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("organization_id", "id", name="uq_roles_organization_id_id"),
    )
    op.create_index(
        "uq_roles_organization_id_name_lower",
        "roles",
        [
            sa.literal_column("(CASE WHEN deleted_at IS NULL THEN `organization_id` END)"),
            sa.literal_column("(CASE WHEN deleted_at IS NULL THEN lower(name) END)"),
        ],
        unique=True,
    )
    op.create_index(
        "uq_roles_organization_id_system_key",
        "roles",
        [
            sa.literal_column("(CASE WHEN system_key IS NOT NULL THEN `organization_id` END)"),
            sa.literal_column("(CASE WHEN system_key IS NOT NULL THEN `system_key` END)"),
        ],
        unique=True,
    )
    op.create_table(
        "segments",
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 40", name=op.f("ck_segments_name_length")
        ),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) BETWEEN 1 AND 500",
            name=op.f("ck_segments_description_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_segments_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_segments")),
        sa.UniqueConstraint("organization_id", "id", name="uq_segments_organization_id_id"),
    )
    op.create_index(
        "uq_segments_organization_id_name_lower",
        "segments",
        ["organization_id", sa.literal_column("(lower(name))")],
        unique=True,
    )
    op.create_table(
        "ticket_tags",
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("color", sa.String(length=255), server_default="grey", nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "color IN ('violet', 'blue', 'green', 'red', 'orange', 'grey')",
            name=op.f("ck_ticket_tags_color"),
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 40", name=op.f("ck_ticket_tags_name_length")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_ticket_tags_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_tags")),
        sa.UniqueConstraint("organization_id", "id", name="uq_ticket_tags_organization_id_id"),
    )
    op.create_index(
        "uq_ticket_tags_organization_id_name_lower",
        "ticket_tags",
        ["organization_id", sa.literal_column("(lower(name))")],
        unique=True,
    )
    op.create_table(
        "time_entry_history",
        sa.Column(
            "time_entry_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "corrected_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("before", sa.JSON(), nullable=False),
        sa.Column("after", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "JSON_TYPE(`before`) = 'OBJECT' AND JSON_TYPE(`after`) = 'OBJECT'",
            name=op.f("ck_time_entry_history_snapshots_are_objects"),
        ),
        sa.CheckConstraint(
            "char_length(trim(reason)) BETWEEN 1 AND 2000",
            name=op.f("ck_time_entry_history_reason_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_time_entry_history_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_time_entry_history")),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_time_entry_history_organization_id_id"
        ),
    )
    op.create_index(
        "ix_time_entry_history_organization_id_time_entry_id_created_at",
        "time_entry_history",
        ["organization_id", "time_entry_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "timesheet_reminders",
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "reminded_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('timesheet', 'approval')", name=op.f("ck_timesheet_reminders_kind")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_timesheet_reminders_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_timesheet_reminders")),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "week_start",
            "kind",
            name="uq_timesheet_reminders_organization_id_user_id_week_start_kind",
        ),
    )
    op.create_index(
        "ix_timesheet_reminders_organization_id_week_start_kind",
        "timesheet_reminders",
        ["organization_id", "week_start", "kind"],
        unique=False,
    )
    op.create_table(
        "vat_rates",
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("rate_bp", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("archived_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "code IN ('standard', 'reduced', 'zero', 'exempt', 'outside_scope')",
            name=op.f("ck_vat_rates_code"),
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 40", name=op.f("ck_vat_rates_name_length")
        ),
        sa.CheckConstraint("rate_bp BETWEEN 0 AND 10000", name=op.f("ck_vat_rates_rate_bp")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_vat_rates_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vat_rates")),
        sa.UniqueConstraint("organization_id", "id", name="uq_vat_rates_organization_id_id"),
    )
    op.create_index(
        "uq_vat_rates_organization_id_default",
        "vat_rates",
        [
            sa.literal_column(
                "(CASE WHEN is_default AND archived_at IS NULL THEN `organization_id` END)"
            )
        ],
        unique=True,
    )
    op.create_index(
        "uq_vat_rates_organization_id_name_lower",
        "vat_rates",
        [
            sa.literal_column("(CASE WHEN archived_at IS NULL THEN `organization_id` END)"),
            sa.literal_column("(CASE WHEN archived_at IS NULL THEN lower(name) END)"),
        ],
        unique=True,
    )
    op.create_table(
        "wip_movements",
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "actor_user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "approval_state", sa.String(length=32), server_default="not_required", nullable=False
        ),
        sa.Column(
            "approved_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("approved_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "reversal_of_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("source_type", sa.String(length=255), nullable=True),
        sa.Column(
            "source_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("item_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("item_refs", sa.JSON(), server_default=sa.text("(JSON_ARRAY())"), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(approval_state = 'approved') = (approved_by IS NOT NULL)",
            name=op.f("ck_wip_movements_approval_fields"),
        ),
        sa.CheckConstraint(
            "(kind = 'reversal') = (reversal_of_id IS NOT NULL)",
            name=op.f("ck_wip_movements_reversal_of"),
        ),
        sa.CheckConstraint(
            "(source_type IS NULL) = (source_id IS NULL) AND (source_type IS NULL OR source_type IN ('adjustment', 'transfer', 'write_off', 'credit_note'))",
            name=op.f("ck_wip_movements_source"),
        ),
        sa.CheckConstraint(
            "JSON_TYPE(item_refs) = 'ARRAY'", name=op.f("ck_wip_movements_item_refs_array")
        ),
        sa.CheckConstraint(
            "approval_state IN ('not_required', 'approved')",
            name=op.f("ck_wip_movements_approval_state"),
        ),
        sa.CheckConstraint(
            "component IN ('time', 'fixed_fee', 'expense')", name=op.f("ck_wip_movements_component")
        ),
        sa.CheckConstraint(
            "kind = 'reversal' OR (kind IN ('write_on', 'transfer_in', 'credit_return') AND amount > 0) OR (kind IN ('write_off', 'transfer_out', 'credit_write_off') AND amount < 0)",
            name=op.f("ck_wip_movements_amount_sign"),
        ),
        sa.CheckConstraint(
            "kind IN ('write_on', 'write_off', 'transfer_out', 'transfer_in', 'credit_return', 'credit_write_off', 'reversal')",
            name=op.f("ck_wip_movements_kind"),
        ),
        sa.CheckConstraint("scope IN ('amount', 'items')", name=op.f("ck_wip_movements_scope")),
        sa.CheckConstraint("amount <> 0", name=op.f("ck_wip_movements_amount_not_zero")),
        sa.CheckConstraint(
            "char_length(trim(reason)) BETWEEN 1 AND 500",
            name=op.f("ck_wip_movements_reason_length"),
        ),
        sa.CheckConstraint("item_count >= 0", name=op.f("ck_wip_movements_item_count")),
        sa.ForeignKeyConstraint(
            ["organization_id", "reversal_of_id"],
            ["wip_movements.organization_id", "wip_movements.id"],
            name="fk_wip_movements_organization_id_reversal_of_id_wip_movements",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_wip_movements_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wip_movements")),
        sa.UniqueConstraint("organization_id", "id", name="uq_wip_movements_organization_id_id"),
    )
    op.create_index(
        "ix_wip_movements_organization_id_client_id",
        "wip_movements",
        ["organization_id", "client_id"],
        unique=False,
    )
    op.create_index(
        "ix_wip_movements_organization_id_created_at",
        "wip_movements",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_wip_movements_organization_id_effective_date",
        "wip_movements",
        ["organization_id", "effective_date", "id"],
        unique=False,
    )
    op.create_index(
        "ix_wip_movements_organization_id_job_id",
        "wip_movements",
        ["organization_id", "job_id"],
        unique=False,
    )
    op.create_index(
        "ix_wip_movements_organization_id_source",
        "wip_movements",
        ["organization_id", "source_type", "source_id"],
        unique=False,
    )
    op.create_index(
        "uq_wip_movements_reversal_of_id",
        "wip_movements",
        [sa.literal_column("(CASE WHEN reversal_of_id IS NOT NULL THEN `reversal_of_id` END)")],
        unique=True,
    )
    op.create_table(
        "wip_snapshots",
        sa.Column("as_at", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=255), server_default="request", nullable=False),
        sa.Column("fixed_fee_wip_mode", sa.String(length=32), nullable=False),
        sa.Column("require_timesheet_approval", sa.Boolean(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("total", sa.BigInteger(), nullable=False),
        sa.Column(
            "taken_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "taken_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "fixed_fee_wip_mode IN ('on_completion', 'by_budget_progress')",
            name=op.f("ck_wip_snapshots_fixed_fee_wip_mode"),
        ),
        sa.CheckConstraint(
            "source IN ('request', 'scheduled')", name=op.f("ck_wip_snapshots_source")
        ),
        sa.CheckConstraint("item_count >= 0", name=op.f("ck_wip_snapshots_item_count")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_wip_snapshots_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wip_snapshots")),
        sa.UniqueConstraint(
            "organization_id", "as_at", name="uq_wip_snapshots_organization_id_as_at"
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_wip_snapshots_organization_id_id"),
    )
    op.create_table(
        "activity_codes",
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("is_chargeable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "vat_rate_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("invoice_description", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(code, '^[A-Z0-9_-]{1,12}$', 'c')",
            name=op.f("ck_activity_codes_code_format"),
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 120", name=op.f("ck_activity_codes_name_length")
        ),
        sa.CheckConstraint(
            "invoice_description IS NULL OR char_length(invoice_description) BETWEEN 1 AND 500",
            name=op.f("ck_activity_codes_invoice_description_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_activity_codes_organization_id_vat_rate_id_vat_rates",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_activity_codes_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_activity_codes")),
        sa.UniqueConstraint("organization_id", "id", name="uq_activity_codes_organization_id_id"),
    )
    op.create_index(
        "uq_activity_codes_organization_id_code",
        "activity_codes",
        ["organization_id", "code"],
        unique=True,
    )
    op.create_index(
        "uq_activity_codes_organization_id_name_lower",
        "activity_codes",
        ["organization_id", sa.literal_column("(lower(name))")],
        unique=True,
    )
    op.create_table(
        "bookkeeping_vat_mappings",
        sa.Column(
            "vat_rate_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("tax_code", sa.Text(), nullable=False),
        sa.Column("tax_name", sa.String(length=120), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(tax_code, '^[A-Za-z0-9][A-Za-z0-9._/-]{0,19}$', 'c')",
            name=op.f("ck_bookkeeping_vat_mappings_tax_code_format"),
        ),
        sa.CheckConstraint(
            "tax_name IS NULL OR char_length(tax_name) BETWEEN 1 AND 120",
            name=op.f("ck_bookkeeping_vat_mappings_tax_name_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_bk_vat_mappings_vat_rate",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_bookkeeping_vat_mappings_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookkeeping_vat_mappings")),
    )
    op.create_index(
        "uq_bk_vat_mappings_default",
        "bookkeeping_vat_mappings",
        [sa.literal_column("(CASE WHEN vat_rate_id IS NULL THEN `organization_id` END)")],
        unique=True,
    )
    op.create_index(
        "uq_bk_vat_mappings_vat_rate",
        "bookkeeping_vat_mappings",
        [
            sa.literal_column("(CASE WHEN vat_rate_id IS NOT NULL THEN `organization_id` END)"),
            sa.literal_column("(CASE WHEN vat_rate_id IS NOT NULL THEN `vat_rate_id` END)"),
        ],
        unique=True,
    )
    op.create_table(
        "data_import_rows",
        sa.Column(
            "batch_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column(
            "target_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("messages", sa.JSON(), server_default=sa.text("(JSON_ARRAY())"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IS NULL OR action IN ('create', 'update', 'unchanged')",
            name=op.f("ck_data_import_rows_action"),
        ),
        sa.CheckConstraint(
            "state = 'rejected' OR action IS NOT NULL",
            name=op.f("ck_data_import_rows_importable_has_action"),
        ),
        sa.CheckConstraint(
            "state IN ('valid', 'warning', 'rejected')", name=op.f("ck_data_import_rows_state")
        ),
        sa.CheckConstraint("`row_number` >= 1", name=op.f("ck_data_import_rows_row_number")),
        sa.ForeignKeyConstraint(
            ["organization_id", "batch_id"],
            ["data_import_batches.organization_id", "data_import_batches.id"],
            name="fk_data_import_rows_organization_id_data_import_batches",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_data_import_rows_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_import_rows")),
        sa.UniqueConstraint(
            "batch_id", "row_number", name="uq_data_import_rows_batch_id_row_number"
        ),
    )
    op.create_table(
        "expense_types",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column(
            "default_vat_rate_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=True,
        ),
        sa.Column("is_mileage", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "default_billable", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(code, '^[A-Z0-9_-]{1,12}$', 'c')",
            name=op.f("ck_expense_types_code_format"),
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 80", name=op.f("ck_expense_types_name_length")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "default_vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_expense_types_default_vat_rate_id_vat_rates",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_expense_types_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expense_types")),
        sa.UniqueConstraint("organization_id", "id", name="uq_expense_types_organization_id_id"),
    )
    op.create_index(
        "uq_expense_types_organization_id_code",
        "expense_types",
        ["organization_id", "code"],
        unique=True,
    )
    op.create_index(
        "uq_expense_types_organization_id_name_lower",
        "expense_types",
        [
            sa.literal_column("(CASE WHEN is_active THEN `organization_id` END)"),
            sa.literal_column("(CASE WHEN is_active THEN lower(name) END)"),
        ],
        unique=True,
    )
    op.create_table(
        "organization_members",
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "role_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column(
            "manager_user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "weekly_capacity_minutes", sa.Integer(), server_default=sa.text("2250"), nullable=False
        ),
        sa.Column(
            "chargeable_target_bp", sa.Integer(), server_default=sa.text("7500"), nullable=False
        ),
        sa.Column("overtime_multiplier_bp", sa.Integer(), nullable=True),
        sa.Column("job_title", sa.Text(), nullable=True),
        sa.Column("initials", sa.String(length=255), nullable=True),
        sa.Column(
            "joined_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("staff_code", sa.String(length=20), nullable=False),
        sa.Column("work_email", sa.String(length=254), nullable=True),
        sa.Column(
            "grade_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "working_minutes_per_day", sa.Integer(), server_default=sa.text("450"), nullable=False
        ),
        sa.Column(
            "working_days",
            sa.JSON(),
            server_default=sa.text("(JSON_ARRAY(1, 2, 3, 4, 5))"),
            nullable=False,
        ),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("leaving_date", sa.Date(), nullable=True),
        sa.Column("login_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("invited_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("mobile_number", sa.String(length=15), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "JSON_TYPE(working_days) = 'ARRAY' AND JSON_LENGTH(working_days) BETWEEN 1 AND 7 AND JSON_CONTAINS(JSON_ARRAY(1, 2, 3, 4, 5, 6, 7), working_days)",
            name=op.f("ck_organization_members_working_days"),
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(staff_code, '^[A-Z0-9_-]{1,20}$', 'c')",
            name=op.f("ck_organization_members_staff_code_format"),
        ),
        sa.CheckConstraint(
            "mobile_number IS NULL OR REGEXP_LIKE(mobile_number, '^[+]?[0-9]{7,15}$', 'c')",
            name=op.f("ck_organization_members_mobile_number_format"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'removed', 'inactive', 'left', 'deleted')",
            name=op.f("ck_organization_members_status"),
        ),
        sa.CheckConstraint(
            "leaving_date IS NULL OR start_date IS NULL OR leaving_date >= start_date",
            name=op.f("ck_organization_members_leaving_after_start"),
        ),
        sa.CheckConstraint(
            "manager_user_id <> user_id", name=op.f("ck_organization_members_not_own_manager")
        ),
        sa.CheckConstraint(
            "overtime_multiplier_bp IS NULL OR overtime_multiplier_bp BETWEEN 10000 AND 100000",
            name=op.f("ck_organization_members_overtime_multiplier_bp"),
        ),
        sa.CheckConstraint(
            "weekly_capacity_minutes BETWEEN 0 AND 10080",
            name=op.f("ck_organization_members_weekly_capacity"),
        ),
        sa.CheckConstraint(
            "work_email IS NULL OR char_length(work_email) BETWEEN 3 AND 254",
            name=op.f("ck_organization_members_work_email_length"),
        ),
        sa.CheckConstraint(
            "working_minutes_per_day BETWEEN 1 AND 1440",
            name=op.f("ck_organization_members_working_minutes"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "grade_id"],
            ["grades.organization_id", "grades.id"],
            name="fk_organization_members_organization_id_grade_id_grades",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "manager_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_organization_members_manager_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "role_id"],
            ["roles.organization_id", "roles.id"],
            name="fk_organization_members_organization_id_role_id_roles",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_organization_members_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_organization_members_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organization_members")),
        sa.UniqueConstraint(
            "organization_id", "user_id", name="uq_organization_members_organization_id_user_id"
        ),
    )
    op.create_index(
        "ix_organization_members_organization_id_grade_id",
        "organization_members",
        ["organization_id", "grade_id"],
        unique=False,
    )
    op.create_index(
        "ix_organization_members_organization_id_manager_user_id",
        "organization_members",
        ["organization_id", "manager_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_organization_members_user_id", "organization_members", ["user_id"], unique=False
    )
    op.create_index(
        "uq_organization_members_organization_id_initials",
        "organization_members",
        [
            sa.literal_column(
                "(CASE WHEN initials IS NOT NULL AND status <> 'removed' THEN `organization_id` END)"
            ),
            sa.literal_column(
                "(CASE WHEN initials IS NOT NULL AND status <> 'removed' THEN upper(initials) END)"
            ),
        ],
        unique=True,
    )
    op.create_index(
        "uq_organization_members_organization_id_staff_code",
        "organization_members",
        ["organization_id", sa.literal_column("(upper(staff_code))")],
        unique=True,
    )
    op.create_index(
        "uq_organization_members_organization_id_work_email_active",
        "organization_members",
        [
            sa.literal_column(
                "(CASE WHEN work_email IS NOT NULL AND status = 'active' THEN `organization_id` END)"
            ),
            sa.literal_column(
                "(CASE WHEN work_email IS NOT NULL AND status = 'active' THEN lower(work_email) END)"
            ),
        ],
        unique=True,
    )
    op.create_table(
        "rate_rerun_items",
        sa.Column("run_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "time_entry_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("old_charge_rate", sa.BigInteger(), nullable=False),
        sa.Column("new_charge_rate", sa.BigInteger(), nullable=False),
        sa.Column("old_charge_value", sa.BigInteger(), nullable=False),
        sa.Column("new_charge_value", sa.BigInteger(), nullable=False),
        sa.Column("old_cost_rate", sa.BigInteger(), nullable=False),
        sa.Column("new_cost_rate", sa.BigInteger(), nullable=False),
        sa.Column("old_cost_value", sa.BigInteger(), nullable=False),
        sa.Column("new_cost_value", sa.BigInteger(), nullable=False),
        sa.Column("old_source", sa.Text(), nullable=True),
        sa.Column("new_source", sa.Text(), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('changed', 'skipped')", name=op.f("ck_rate_rerun_items_outcome")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["rate_reruns.organization_id", "rate_reruns.id"],
            name="fk_rate_rerun_items_organization_id_run_id_rate_reruns",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_rate_rerun_items_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_rerun_items")),
    )
    op.create_index(
        "ix_rate_rerun_items_organization_id_run_id",
        "rate_rerun_items",
        ["organization_id", "run_id"],
        unique=False,
    )
    op.create_table(
        "role_permissions",
        sa.Column(
            "role_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "permission_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name=op.f("fk_role_permissions_permission_id_permissions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_role_permissions_role_id_roles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_id", name=op.f("pk_role_permissions")),
    )
    op.create_table(
        "services",
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("billing_basis", sa.String(length=32), server_default="hourly", nullable=False),
        sa.Column("default_fee", sa.BigInteger(), nullable=True),
        sa.Column("default_billable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "vat_rate_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("invoice_description", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(code, '^[A-Z0-9_-]{1,12}$', 'c')", name=op.f("ck_services_code_format")
        ),
        sa.CheckConstraint(
            "billing_basis IN ('hourly', 'fixed')", name=op.f("ck_services_billing_basis")
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 120", name=op.f("ck_services_name_length")
        ),
        sa.CheckConstraint(
            "default_fee IS NULL OR default_fee >= 0", name=op.f("ck_services_default_fee")
        ),
        sa.CheckConstraint(
            "invoice_description IS NULL OR char_length(invoice_description) BETWEEN 1 AND 500",
            name=op.f("ck_services_invoice_description_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_services_organization_id_vat_rate_id_vat_rates",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_services_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_services")),
        sa.UniqueConstraint("organization_id", "id", name="uq_services_organization_id_id"),
    )
    op.create_index(
        "uq_services_organization_id_code", "services", ["organization_id", "code"], unique=True
    )
    op.create_index(
        "uq_services_organization_id_name_lower",
        "services",
        ["organization_id", sa.literal_column("(lower(name))")],
        unique=True,
    )
    op.create_table(
        "wip_snapshot_items",
        sa.Column(
            "snapshot_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column(
            "item_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("client_code", sa.Text(), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=False),
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("job_number", sa.Text(), nullable=True),
        sa.Column("job_name", sa.Text(), nullable=True),
        sa.Column("billing_basis", sa.Text(), nullable=True),
        sa.Column(
            "partner_user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "manager_user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "service_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "person_user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("item_date", sa.Date(), nullable=False),
        sa.Column("minutes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("value", sa.BigInteger(), nullable=False),
        sa.Column("charge_value", sa.BigInteger(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "component IN ('time', 'fixed_fee', 'expense')",
            name=op.f("ck_wip_snapshot_items_component"),
        ),
        sa.CheckConstraint(
            "kind IN ('time', 'fixed_fee', 'expense', 'disbursement', 'adjustment')",
            name=op.f("ck_wip_snapshot_items_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "snapshot_id"],
            ["wip_snapshots.organization_id", "wip_snapshots.id"],
            name="fk_wip_snapshot_items_snapshot_id_wip_snapshots",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_wip_snapshot_items_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wip_snapshot_items")),
    )
    op.create_index(
        "ix_wip_snapshot_items_snapshot_id_client_id_job_id",
        "wip_snapshot_items",
        ["snapshot_id", "client_id", "job_id"],
        unique=False,
    )
    op.create_index(
        "ix_wip_snapshot_items_snapshot_id_item_date",
        "wip_snapshot_items",
        ["snapshot_id", "item_date", "id"],
        unique=False,
    )
    op.create_table(
        "bookkeeping_service_mappings",
        sa.Column(
            "service_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("account_code", sa.Text(), nullable=False),
        sa.Column("account_name", sa.String(length=120), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(account_code, '^[A-Za-z0-9][A-Za-z0-9._/-]{0,19}$', 'c')",
            name=op.f("ck_bookkeeping_service_mappings_account_code_format"),
        ),
        sa.CheckConstraint(
            "account_name IS NULL OR char_length(account_name) BETWEEN 1 AND 120",
            name=op.f("ck_bookkeeping_service_mappings_account_name_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_bk_service_mappings_service",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_bookkeeping_service_mappings_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookkeeping_service_mappings")),
    )
    op.create_index(
        "uq_bk_service_mappings_default",
        "bookkeeping_service_mappings",
        [sa.literal_column("(CASE WHEN service_id IS NULL THEN `organization_id` END)")],
        unique=True,
    )
    op.create_index(
        "uq_bk_service_mappings_service",
        "bookkeeping_service_mappings",
        [
            sa.literal_column("(CASE WHEN service_id IS NOT NULL THEN `organization_id` END)"),
            sa.literal_column("(CASE WHEN service_id IS NOT NULL THEN `service_id` END)"),
        ],
        unique=True,
    )
    op.create_table(
        "clients",
        sa.Column("code", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "client_type", sa.String(length=32), server_default="limited_company", nullable=False
        ),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("address_line1", sa.Text(), nullable=True),
        sa.Column("address_line2", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("postcode", sa.Text(), nullable=True),
        sa.Column("country", sa.String(length=255), server_default="GB", nullable=False),
        sa.Column("company_number", sa.Text(), nullable=True),
        sa.Column("utr", sa.Text(), nullable=True),
        sa.Column("vat_number", sa.Text(), nullable=True),
        sa.Column("year_end_month", sa.SmallInteger(), nullable=True),
        sa.Column("year_end_day", sa.SmallInteger(), nullable=True),
        sa.Column(
            "partner_user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "manager_user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("payment_terms_days", sa.SmallInteger(), nullable=True),
        sa.Column(
            "default_vat_rate_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=True,
        ),
        sa.Column("cost_plus_markup_bp", sa.Integer(), nullable=True),
        sa.Column("billing_notes", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("segment", sa.String(length=40), nullable=True),
        sa.Column(
            "branch_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("aml_status", sa.String(length=32), server_default="not_started", nullable=False),
        sa.Column("aml_checked_on", sa.Date(), nullable=True),
        sa.Column("health_score", sa.SmallInteger(), nullable=True),
        sa.Column("accounting_software", sa.String(length=32), nullable=True),
        sa.Column("invoice_email", sa.String(length=254), nullable=True),
        sa.Column(
            "default_service_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=True,
        ),
        sa.Column(
            "invoice_template_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=True,
        ),
        sa.Column("central_client_id", sa.String(length=64), nullable=True),
        sa.Column("central_synced_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "central_sync_state", sa.String(length=32), server_default="local", nullable=False
        ),
        sa.Column(
            "merged_into_client_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=True,
        ),
        sa.Column("is_internal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.CheckConstraint(
            "(central_client_id IS NULL) = (central_sync_state = 'local')",
            name=op.f("ck_clients_central_state_matches_link"),
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(code, '^[A-Z0-9_-]{1,20}$', 'c')", name=op.f("ck_clients_code_format")
        ),
        sa.CheckConstraint(
            "accounting_software IS NULL OR accounting_software IN ('xero', 'quickbooks', 'sage', 'freeagent', 'other')",
            name=op.f("ck_clients_accounting_software"),
        ),
        sa.CheckConstraint(
            "aml_status IN ('not_started', 'pending', 'clear', 'review', 'failed')",
            name=op.f("ck_clients_aml_status"),
        ),
        sa.CheckConstraint(
            "central_sync_state IN ('local', 'synced', 'stale', 'failed')",
            name=op.f("ck_clients_central_sync_state"),
        ),
        sa.CheckConstraint(
            "client_type IN ('individual', 'sole_trader', 'partnership', 'llp', 'limited_company', 'trust', 'charity', 'other')",
            name=op.f("ck_clients_client_type"),
        ),
        sa.CheckConstraint(
            "status IN ('prospect', 'active', 'inactive', 'archived')",
            name=op.f("ck_clients_status"),
        ),
        sa.CheckConstraint(
            "(year_end_month IS NULL) = (year_end_day IS NULL)",
            name=op.f("ck_clients_year_end_complete"),
        ),
        sa.CheckConstraint(
            "central_client_id IS NULL OR char_length(central_client_id) BETWEEN 1 AND 64",
            name=op.f("ck_clients_central_client_id_length"),
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 200", name=op.f("ck_clients_name_length")
        ),
        sa.CheckConstraint(
            "cost_plus_markup_bp IS NULL OR cost_plus_markup_bp BETWEEN 0 AND 100000",
            name=op.f("ck_clients_cost_plus_markup_bp"),
        ),
        sa.CheckConstraint("health_score BETWEEN 0 AND 100", name=op.f("ck_clients_health_score")),
        sa.CheckConstraint(
            "invoice_email IS NULL OR char_length(invoice_email) BETWEEN 3 AND 254",
            name=op.f("ck_clients_invoice_email_length"),
        ),
        sa.CheckConstraint(
            "merged_into_client_id IS NULL OR merged_into_client_id <> id",
            name=op.f("ck_clients_not_merged_into_itself"),
        ),
        sa.CheckConstraint(
            "payment_terms_days IS NULL OR payment_terms_days BETWEEN 0 AND 365",
            name=op.f("ck_clients_payment_terms_days"),
        ),
        sa.CheckConstraint(
            "segment IS NULL OR char_length(segment) BETWEEN 1 AND 40",
            name=op.f("ck_clients_segment"),
        ),
        sa.CheckConstraint("year_end_day BETWEEN 1 AND 31", name=op.f("ck_clients_year_end_day")),
        sa.CheckConstraint(
            "year_end_month BETWEEN 1 AND 12", name=op.f("ck_clients_year_end_month")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "branch_id"],
            ["branches.organization_id", "branches.id"],
            name="fk_clients_organization_id_branch_id_branches",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "default_service_id"],
            ["services.organization_id", "services.id"],
            name="fk_clients_organization_id_default_service_id_services",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "default_vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_clients_organization_id_default_vat_rate_id_vat_rates",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "invoice_template_id"],
            ["invoice_templates.organization_id", "invoice_templates.id"],
            name="fk_clients_invoice_template_invoice_templates",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "manager_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_clients_organization_id_manager_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "merged_into_client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_clients_organization_id_merged_into_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "partner_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_clients_organization_id_partner_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_clients_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_clients")),
        sa.UniqueConstraint("organization_id", "id", name="uq_clients_organization_id_id"),
    )
    op.create_index(
        "ix_clients_organization_id_branch_id",
        "clients",
        ["organization_id", "branch_id"],
        unique=False,
    )
    op.create_index(
        "ix_clients_organization_id_manager_user_id",
        "clients",
        ["organization_id", "manager_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_clients_organization_id_merged_into_client_id",
        "clients",
        ["organization_id", "merged_into_client_id"],
        unique=False,
    )
    op.create_index(
        "ix_clients_organization_id_name_lower",
        "clients",
        ["organization_id", sa.literal_column("(lower(name))")],
        unique=False,
    )
    op.create_index(
        "uq_clients_organization_id_central_client_id",
        "clients",
        [
            sa.literal_column(
                "(CASE WHEN central_client_id IS NOT NULL AND deleted_at IS NULL THEN `organization_id` END)"
            ),
            sa.literal_column(
                "(CASE WHEN central_client_id IS NOT NULL AND deleted_at IS NULL THEN `central_client_id` END)"
            ),
        ],
        unique=True,
    )
    op.create_index(
        "uq_clients_organization_id_code",
        "clients",
        [
            sa.literal_column("(CASE WHEN deleted_at IS NULL THEN `organization_id` END)"),
            sa.literal_column("(CASE WHEN deleted_at IS NULL THEN `code` END)"),
        ],
        unique=True,
    )
    op.create_index(
        "uq_clients_organization_id_internal",
        "clients",
        [sa.literal_column("(CASE WHEN is_internal THEN `organization_id` END)")],
        unique=True,
    )
    op.create_table(
        "expense_claims",
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="draft", nullable=False),
        sa.Column("submitted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("decided_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "decided_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'draft' AND submitted_at IS NULL AND decided_at IS NULL AND decided_by IS NULL) OR (status = 'submitted' AND submitted_at IS NOT NULL AND decided_at IS NULL AND decided_by IS NULL) OR (status IN ('approved', 'returned') AND submitted_at IS NOT NULL AND decided_at IS NOT NULL AND decided_by IS NOT NULL)",
            name=op.f("ck_expense_claims_status_fields"),
        ),
        sa.CheckConstraint(
            "status <> 'returned' OR decision_note IS NOT NULL",
            name=op.f("ck_expense_claims_return_note"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'submitted', 'approved', 'returned')",
            name=op.f("ck_expense_claims_status"),
        ),
        sa.CheckConstraint(
            "decision_note IS NULL OR char_length(decision_note) BETWEEN 1 AND 2000",
            name=op.f("ck_expense_claims_decision_note_length"),
        ),
        sa.CheckConstraint(
            "title IS NULL OR char_length(title) BETWEEN 1 AND 200",
            name=op.f("ck_expense_claims_title_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_expense_claims_organization_id_created_by_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "decided_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_expense_claims_organization_id_decided_by_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_expense_claims_organization_id_user_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_expense_claims_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expense_claims")),
        sa.UniqueConstraint("organization_id", "id", name="uq_expense_claims_organization_id_id"),
    )
    op.create_index(
        "ix_expense_claims_organization_id_created_at",
        "expense_claims",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_expense_claims_organization_id_user_id_created_at",
        "expense_claims",
        ["organization_id", "user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_expense_claims_submitted",
        "expense_claims",
        ["organization_id", "submitted_at"],
        unique=False,
    )
    op.create_table(
        "member_permission_overrides",
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("permission_code", sa.String(length=255), nullable=False),
        sa.Column("effect", sa.String(length=32), nullable=False),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.CheckConstraint(
            "effect IN ('grant', 'deny')", name=op.f("ck_member_permission_overrides_effect")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_member_permission_overrides_organization_members",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["permission_code"],
            ["permissions.code"],
            name=op.f("fk_member_permission_overrides_permission_code_permissions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_member_permission_overrides")),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "permission_code",
            name="uq_member_permission_overrides_member_code",
        ),
    )
    op.create_table(
        "notification_preferences",
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("type", sa.String(length=49), nullable=False),
        sa.Column("in_app", sa.Boolean(), nullable=False),
        sa.Column("email", sa.Boolean(), nullable=False),
        sa.Column("sms", sa.Boolean(), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(type, '^[a-z][a-z0-9_]{1,49}$', 'c')",
            name=op.f("ck_notification_preferences_type_format"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_notification_preferences_user_organization_members",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_notification_preferences_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_preferences")),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "type",
            name="uq_notification_preferences_organization_id_user_id_type",
        ),
    )
    op.create_table(
        "notifications",
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("type", sa.String(length=49), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), server_default=sa.text("('')"), nullable=False),
        sa.Column("link", sa.String(length=300), nullable=True),
        sa.Column("entity_type", sa.String(length=50), nullable=True),
        sa.Column(
            "entity_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("payload", sa.JSON(), server_default=sa.text("(JSON_OBJECT())"), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=True),
        sa.Column("read_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(type, '^[a-z][a-z0-9_]{1,49}$', 'c')",
            name=op.f("ck_notifications_type_format"),
        ),
        sa.CheckConstraint(
            "link IS NULL OR (char_length(link) <= 300 AND REGEXP_LIKE(link, '^/($|[^/])', 'c') AND NOT REGEXP_LIKE(link, '[[:space:][:cntrl:]]', 'c') AND INSTR(link, CHAR(92 USING utf8mb4)) = 0)",
            name=op.f("ck_notifications_link_relative"),
        ),
        sa.CheckConstraint(
            "(entity_type IS NULL) = (entity_id IS NULL)", name=op.f("ck_notifications_entity_pair")
        ),
        sa.CheckConstraint("char_length(body) <= 2000", name=op.f("ck_notifications_body_length")),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 200", name=op.f("ck_notifications_title_length")
        ),
        sa.CheckConstraint(
            "dedupe_key IS NULL OR char_length(dedupe_key) BETWEEN 1 AND 200",
            name=op.f("ck_notifications_dedupe_key_length"),
        ),
        sa.CheckConstraint(
            "entity_type IS NULL OR char_length(entity_type) BETWEEN 1 AND 50",
            name=op.f("ck_notifications_entity_type_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_notifications_organization_id_user_id_organization_members",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_notifications_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("organization_id", "id", name="uq_notifications_organization_id_id"),
    )
    op.create_index(
        "ix_notifications_organization_id_user_id_created_at",
        "notifications",
        ["organization_id", "user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_unread", "notifications", ["organization_id", "user_id"], unique=False
    )
    op.create_index(
        "uq_notifications_organization_id_user_id_dedupe_key",
        "notifications",
        [
            sa.literal_column("(CASE WHEN dedupe_key IS NOT NULL THEN `organization_id` END)"),
            sa.literal_column("(CASE WHEN dedupe_key IS NOT NULL THEN `user_id` END)"),
            sa.literal_column("(CASE WHEN dedupe_key IS NOT NULL THEN `dedupe_key` END)"),
        ],
        unique=True,
    )
    op.create_table(
        "overtime_rates",
        sa.Column("level", sa.String(length=32), nullable=False),
        sa.Column(
            "grade_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("multiplier_bp", sa.Integer(), nullable=True),
        sa.Column("rate", sa.BigInteger(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(level = 'grade' AND grade_id IS NOT NULL AND user_id IS NULL) OR (level = 'staff' AND user_id IS NOT NULL AND grade_id IS NULL)",
            name=op.f("ck_overtime_rates_scope"),
        ),
        sa.CheckConstraint(
            "(mode = 'multiplier' AND multiplier_bp BETWEEN 10000 AND 100000 AND rate IS NULL) OR (mode = 'fixed' AND rate >= 0 AND multiplier_bp IS NULL)",
            name=op.f("ck_overtime_rates_amount"),
        ),
        sa.CheckConstraint("level IN ('grade', 'staff')", name=op.f("ck_overtime_rates_level")),
        sa.CheckConstraint("mode IN ('multiplier', 'fixed')", name=op.f("ck_overtime_rates_mode")),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_overtime_rates_period"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_overtime_rates_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "grade_id"],
            ["grades.organization_id", "grades.id"],
            name="fk_overtime_rates_organization_id_grade_id_grades",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_overtime_rates_organization_id_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_overtime_rates_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_overtime_rates")),
    )
    op.create_table(
        "period_lock_events",
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("period_type", sa.String(length=32), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("previous_locked_through", sa.Date(), nullable=True),
        sa.Column("locked_through", sa.Date(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column(
            "actor_user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type <> 'reopened' OR reason IS NOT NULL",
            name=op.f("ck_period_lock_events_reopen_reason"),
        ),
        sa.CheckConstraint(
            "event_type IN ('locked', 'reopened')", name=op.f("ck_period_lock_events_event_type")
        ),
        sa.CheckConstraint(
            "period_type IS NULL OR (period_type IN ('week', 'month', 'quarter', 'year', 'legacy') AND period_start IS NOT NULL AND period_end IS NOT NULL)",
            name=op.f("ck_period_lock_events_period"),
        ),
        sa.CheckConstraint(
            "reason IS NULL OR char_length(reason) BETWEEN 1 AND 500",
            name=op.f("ck_period_lock_events_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_period_lock_events_organization_id_actor_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_period_lock_events_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_period_lock_events")),
    )
    op.create_index(
        "ix_period_lock_events_organization_id_created_at",
        "period_lock_events",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "service_activity_codes",
        sa.Column(
            "service_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "activity_code_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "activity_code_id"],
            ["activity_codes.organization_id", "activity_codes.id"],
            name="fk_service_activity_codes_activity_code_id_activity_codes",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_service_activity_codes_service_id_services",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_service_activity_codes_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "service_id", "activity_code_id", name=op.f("pk_service_activity_codes")
        ),
    )
    op.create_index(
        "ix_service_activity_codes_organization_id_activity_code_id",
        "service_activity_codes",
        ["organization_id", "activity_code_id"],
        unique=False,
    )
    op.create_table(
        "service_task_templates",
        sa.Column(
            "service_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("budget_minutes", sa.Integer(), nullable=True),
        sa.Column("due_offset_days", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "budget_minutes IS NULL OR budget_minutes >= 0",
            name=op.f("ck_service_task_templates_budget_minutes"),
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 200",
            name=op.f("ck_service_task_templates_title_length"),
        ),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) <= 2000",
            name=op.f("ck_service_task_templates_description_length"),
        ),
        sa.CheckConstraint(
            "due_offset_days IS NULL OR due_offset_days BETWEEN 0 AND 3650",
            name=op.f("ck_service_task_templates_due_offset_days"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_service_task_templates_service_id_services",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_service_task_templates_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_service_task_templates")),
    )
    op.create_index(
        "ix_service_task_templates_organization_id_service_id_sort_order",
        "service_task_templates",
        ["organization_id", "service_id", "sort_order"],
        unique=False,
    )
    op.create_table(
        "staff_hours_bases",
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("basis", sa.String(length=32), nullable=False),
        sa.Column("standard_minutes", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "basis IN ('weekly', 'monthly')", name=op.f("ck_staff_hours_bases_basis")
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_staff_hours_bases_period"),
        ),
        sa.CheckConstraint(
            "standard_minutes > 0", name=op.f("ck_staff_hours_bases_standard_minutes")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_staff_hours_bases_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_staff_hours_bases_organization_members",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_staff_hours_bases_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_hours_bases")),
    )
    op.create_table(
        "staff_segments",
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "segment_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "segment_id"],
            ["segments.organization_id", "segments.id"],
            name="fk_staff_segments_organization_id_segment_id_segments",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_staff_segments_organization_id_user_id_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_staff_segments_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_segments")),
        sa.UniqueConstraint(
            "organization_id", "user_id", "segment_id", name="uq_staff_segments_user_id_segment_id"
        ),
    )
    op.create_index(
        "ix_staff_segments_organization_id_segment_id",
        "staff_segments",
        ["organization_id", "segment_id"],
        unique=False,
    )
    op.create_table(
        "tickets",
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=30), nullable=False),
        sa.Column("category", sa.String(length=255), server_default="application", nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("description_html", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=255), server_default="open", nullable=False),
        sa.Column("priority", sa.String(length=255), server_default="not_set", nullable=False),
        sa.Column(
            "assignee_user_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=True,
        ),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("closed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL)", name=op.f("ck_tickets_closed_at")
        ),
        sa.CheckConstraint("category IN ('application')", name=op.f("ck_tickets_category")),
        sa.CheckConstraint(
            "priority IN ('high', 'medium', 'low', 'not_set')", name=op.f("ck_tickets_priority")
        ),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'requirement_pending', 'clarification_required', 'blocked', 'ready_for_ba_review', 'reopened', 'closed', 'invalid', 'on_hold')",
            name=op.f("ck_tickets_status"),
        ),
        sa.CheckConstraint(
            "char_length(code) BETWEEN 1 AND 30", name=op.f("ck_tickets_code_length")
        ),
        sa.CheckConstraint(
            "char_length(subject) BETWEEN 1 AND 200", name=op.f("ck_tickets_subject_length")
        ),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) <= 10000",
            name=op.f("ck_tickets_description_length"),
        ),
        sa.CheckConstraint(
            "description_html IS NULL OR char_length(description_html) <= 40000",
            name=op.f("ck_tickets_description_html_length"),
        ),
        sa.CheckConstraint("number >= 1", name=op.f("ck_tickets_number")),
        sa.ForeignKeyConstraint(
            ["organization_id", "assignee_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_tickets_organization_id_assignee_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_tickets_organization_id_created_by_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_tickets_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
        sa.UniqueConstraint("organization_id", "id", name="uq_tickets_organization_id_id"),
    )
    op.create_index(
        "ix_tickets_organization_id_assignee_user_id",
        "tickets",
        ["organization_id", "assignee_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_tickets_organization_id_created_at",
        "tickets",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_tickets_organization_id_priority",
        "tickets",
        ["organization_id", "priority", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_tickets_organization_id_status",
        "tickets",
        ["organization_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_tickets_organization_id_subject_lower",
        "tickets",
        ["organization_id", sa.literal_column("(lower(subject))"), "id"],
        unique=False,
    )
    op.create_index(
        "uq_tickets_organization_id_code", "tickets", ["organization_id", "code"], unique=True
    )
    op.create_index(
        "uq_tickets_organization_id_number", "tickets", ["organization_id", "number"], unique=True
    )
    op.create_table(
        "timesheets",
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="open", nullable=False),
        sa.Column("submitted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "decided_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("decided_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("approval_exempt", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
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
        "client_contacts",
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("position", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_billing", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 120", name=op.f("ck_client_contacts_name_length")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_client_contacts_organization_id_client_id_clients",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_client_contacts_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_contacts")),
    )
    op.create_index(
        "ix_client_contacts_organization_id_client_id",
        "client_contacts",
        ["organization_id", "client_id"],
        unique=False,
    )
    op.create_index(
        "uq_client_contacts_client_id_primary",
        "client_contacts",
        [sa.literal_column("(CASE WHEN is_primary THEN `client_id` END)")],
        unique=True,
    )
    op.create_table(
        "client_services",
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "service_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("fee", sa.BigInteger(), nullable=True),
        sa.Column("frequency", sa.String(length=32), server_default="annually", nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "auto_create_jobs", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("job_lead_days", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "frequency IN ('one_off', 'monthly', 'quarterly', 'annually')",
            name=op.f("ck_client_services_frequency"),
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR start_date <= end_date",
            name=op.f("ck_client_services_date_order"),
        ),
        sa.CheckConstraint("fee IS NULL OR fee >= 0", name=op.f("ck_client_services_fee")),
        sa.CheckConstraint(
            "job_lead_days BETWEEN 0 AND 365", name=op.f("ck_client_services_job_lead_days")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_client_services_organization_id_client_id_clients",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_client_services_organization_id_service_id_services",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_client_services_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_services")),
        sa.UniqueConstraint(
            "client_id", "service_id", name="uq_client_services_client_id_service_id"
        ),
    )
    op.create_index(
        "ix_client_services_organization_id_service_id",
        "client_services",
        ["organization_id", "service_id"],
        unique=False,
    )
    op.create_table(
        "expense_claim_events",
        sa.Column(
            "claim_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column(
            "actor_user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('submitted', 'recalled', 'approved', 'returned', 'corrected')",
            name=op.f("ck_expense_claim_events_action"),
        ),
        sa.CheckConstraint(
            "note IS NULL OR char_length(note) BETWEEN 1 AND 2000",
            name=op.f("ck_expense_claim_events_note_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_expense_claim_events_organization_id_actor_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "claim_id"],
            ["expense_claims.organization_id", "expense_claims.id"],
            name="fk_expense_claim_events_claim_id_expense_claims",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_expense_claim_events_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expense_claim_events")),
    )
    op.create_index(
        "ix_expense_claim_events_organization_id_claim_id_at",
        "expense_claim_events",
        ["organization_id", "claim_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "invoices",
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("number", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="draft", nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("net_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("vat_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("gross_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("paid_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("credited_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("issued_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "issued_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("sent_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("voided_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "voided_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("void_reason", sa.Text(), nullable=True),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "deleted_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status IN ('draft', 'awaiting_approval', 'approved')) = (number IS NULL)",
            name=op.f("ck_invoices_number_when_issued"),
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(currency, '^[A-Z]{3}$', 'c')", name=op.f("ck_invoices_currency_format")
        ),
        sa.CheckConstraint(
            "deleted_at IS NULL OR status = 'void'", name=op.f("ck_invoices_deleted_only_void")
        ),
        sa.CheckConstraint(
            "status <> 'void' OR paid_total = 0", name=op.f("ck_invoices_void_has_no_payments")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'awaiting_approval', 'approved') OR (issue_date IS NOT NULL AND due_date IS NOT NULL)",
            name=op.f("ck_invoices_dates_when_issued"),
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'awaiting_approval', 'approved', 'issued', 'void')",
            name=op.f("ck_invoices_status"),
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
            ["deleted_by"], ["users.id"], name=op.f("fk_invoices_deleted_by_users")
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
        [
            sa.literal_column("(CASE WHEN number IS NOT NULL THEN `organization_id` END)"),
            sa.literal_column("(CASE WHEN number IS NOT NULL THEN `number` END)"),
        ],
        unique=True,
    )
    op.create_table(
        "jobs",
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "service_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("number", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="planned", nullable=False),
        sa.Column("billing_basis", sa.String(length=32), server_default="hourly", nullable=False),
        sa.Column("fixed_fee", sa.BigInteger(), nullable=True),
        sa.Column("hourly_rate", sa.BigInteger(), nullable=True),
        sa.Column("cost_plus_markup_bp", sa.Integer(), nullable=True),
        sa.Column("budget_minutes", sa.Integer(), nullable=True),
        sa.Column("budget_fee", sa.BigInteger(), nullable=True),
        sa.Column("budget_cost", sa.BigInteger(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column(
            "partner_user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "manager_user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("completed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("closed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("pm_job_id", sa.String(length=64), nullable=True),
        sa.Column("pm_linked_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("deleted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.CheckConstraint(
            "billing_basis <> 'fixed' OR fixed_fee IS NOT NULL",
            name=op.f("ck_jobs_fixed_fee_required"),
        ),
        sa.CheckConstraint(
            "billing_basis IN ('hourly', 'fixed', 'non_billable')",
            name=op.f("ck_jobs_billing_basis"),
        ),
        sa.CheckConstraint(
            "closed_at IS NULL OR status = 'closed'", name=op.f("ck_jobs_closed_at_status")
        ),
        sa.CheckConstraint(
            "status IN ('planned', 'in_progress', 'on_hold', 'completed', 'closed', 'cancelled')",
            name=op.f("ck_jobs_status"),
        ),
        sa.CheckConstraint(
            "(pm_job_id IS NULL) = (pm_linked_at IS NULL)", name=op.f("ck_jobs_pm_link")
        ),
        sa.CheckConstraint(
            "budget_cost IS NULL OR budget_cost >= 0", name=op.f("ck_jobs_budget_cost")
        ),
        sa.CheckConstraint(
            "budget_fee IS NULL OR budget_fee >= 0", name=op.f("ck_jobs_budget_fee")
        ),
        sa.CheckConstraint(
            "budget_minutes IS NULL OR budget_minutes >= 0", name=op.f("ck_jobs_budget_minutes")
        ),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 200", name=op.f("ck_jobs_name_length")),
        sa.CheckConstraint(
            "cost_plus_markup_bp IS NULL OR cost_plus_markup_bp BETWEEN 0 AND 100000",
            name=op.f("ck_jobs_cost_plus_markup_bp"),
        ),
        sa.CheckConstraint("fixed_fee IS NULL OR fixed_fee >= 0", name=op.f("ck_jobs_fixed_fee")),
        sa.CheckConstraint(
            "hourly_rate IS NULL OR hourly_rate >= 0", name=op.f("ck_jobs_hourly_rate")
        ),
        sa.CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_start <= period_end",
            name=op.f("ck_jobs_period_order"),
        ),
        sa.CheckConstraint(
            "pm_job_id IS NULL OR char_length(pm_job_id) BETWEEN 1 AND 64",
            name=op.f("ck_jobs_pm_job_id_length"),
        ),
        sa.CheckConstraint(
            "start_date IS NULL OR due_date IS NULL OR start_date <= due_date",
            name=op.f("ck_jobs_date_order"),
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
    op.create_index(
        "uq_jobs_organization_id_pm_job_id",
        "jobs",
        [
            sa.literal_column(
                "(CASE WHEN pm_job_id IS NOT NULL AND deleted_at IS NULL THEN `organization_id` END)"
            ),
            sa.literal_column(
                "(CASE WHEN pm_job_id IS NOT NULL AND deleted_at IS NULL THEN `pm_job_id` END)"
            ),
        ],
        unique=True,
    )
    op.create_table(
        "leads",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "client_type", sa.String(length=32), server_default="limited_company", nullable=False
        ),
        sa.Column("contact_name", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("acquisition_cost", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("onboarding_cost", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("archived_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "converted_client_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=True,
        ),
        sa.Column("converted_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "client_type IN ('individual', 'sole_trader', 'partnership', 'llp', 'limited_company', 'trust', 'charity', 'other')",
            name=op.f("ck_leads_client_type"),
        ),
        sa.CheckConstraint(
            "(converted_client_id IS NULL) = (converted_at IS NULL)",
            name=op.f("ck_leads_converted_together"),
        ),
        sa.CheckConstraint("acquisition_cost >= 0", name=op.f("ck_leads_acquisition_cost")),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 200", name=op.f("ck_leads_name_length")
        ),
        sa.CheckConstraint("onboarding_cost >= 0", name=op.f("ck_leads_onboarding_cost")),
        sa.CheckConstraint(
            "source IS NULL OR char_length(source) BETWEEN 1 AND 100",
            name=op.f("ck_leads_source_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "converted_client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_leads_organization_id_converted_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_leads_organization_id_created_by_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_leads_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leads")),
        sa.UniqueConstraint("organization_id", "id", name="uq_leads_organization_id_id"),
    )
    op.create_index(
        "ix_leads_organization_id_created_at",
        "leads",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "uq_leads_organization_id_converted_client_id",
        "leads",
        [
            sa.literal_column(
                "(CASE WHEN converted_client_id IS NOT NULL THEN `organization_id` END)"
            ),
            sa.literal_column(
                "(CASE WHEN converted_client_id IS NOT NULL THEN `converted_client_id` END)"
            ),
        ],
        unique=True,
    )
    op.create_table(
        "payments",
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("received_on", sa.Date(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.String(length=32), server_default="bank_transfer", nullable=False),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("allocated_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("voided_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("reversed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "reversed_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("reversal_reason", sa.String(length=500), nullable=True),
        sa.Column("bounced", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "method IN ('bank_transfer', 'card', 'direct_debit', 'cash', 'cheque', 'other')",
            name=op.f("ck_payments_method"),
        ),
        sa.CheckConstraint(
            "(reversed_at IS NULL) = (reversed_by IS NULL) AND (reversed_at IS NULL) = (reversal_reason IS NULL)",
            name=op.f("ck_payments_reversal_fields"),
        ),
        sa.CheckConstraint(
            "NOT bounced OR reversed_at IS NOT NULL", name=op.f("ck_payments_bounced_is_reversed")
        ),
        sa.CheckConstraint(
            "allocated_total >= 0 AND allocated_total <= amount",
            name=op.f("ck_payments_allocated_total"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payments_amount")),
        sa.CheckConstraint(
            "reversal_reason IS NULL OR char_length(reversal_reason) BETWEEN 1 AND 500",
            name=op.f("ck_payments_reversal_reason_length"),
        ),
        sa.CheckConstraint(
            "reversed_at IS NULL OR voided_at IS NOT NULL",
            name=op.f("ck_payments_reversed_is_void"),
        ),
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
        sa.ForeignKeyConstraint(
            ["reversed_by"], ["users.id"], name=op.f("fk_payments_reversed_by_users")
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
        "rate_rules",
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("level", sa.String(length=32), nullable=False),
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "service_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column(
            "grade_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("rate", sa.BigInteger(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(level = 'client_service_staff' AND client_id IS NOT NULL AND service_id IS NOT NULL AND user_id IS NOT NULL AND grade_id IS NULL) OR (level = 'client_service' AND client_id IS NOT NULL AND service_id IS NOT NULL AND user_id IS NULL AND grade_id IS NULL) OR (level = 'client_staff' AND client_id IS NOT NULL AND service_id IS NULL AND user_id IS NOT NULL AND grade_id IS NULL) OR (level = 'client' AND client_id IS NOT NULL AND service_id IS NULL AND user_id IS NULL AND grade_id IS NULL) OR (level = 'service_staff' AND client_id IS NULL AND service_id IS NOT NULL AND user_id IS NOT NULL AND grade_id IS NULL) OR (level = 'service_grade' AND client_id IS NULL AND service_id IS NOT NULL AND user_id IS NULL AND grade_id IS NOT NULL) OR (level = 'service' AND client_id IS NULL AND service_id IS NOT NULL AND user_id IS NULL AND grade_id IS NULL) OR (level = 'staff' AND client_id IS NULL AND service_id IS NULL AND user_id IS NOT NULL AND grade_id IS NULL) OR (level = 'grade' AND client_id IS NULL AND service_id IS NULL AND user_id IS NULL AND grade_id IS NOT NULL)",
            name=op.f("ck_rate_rules_scope"),
        ),
        sa.CheckConstraint(
            "kind = 'charge' OR level IN ('staff', 'grade')", name=op.f("ck_rate_rules_cost_level")
        ),
        sa.CheckConstraint("kind IN ('charge', 'cost')", name=op.f("ck_rate_rules_kind")),
        sa.CheckConstraint(
            "level IN ('client_service_staff', 'client_service', 'client_staff', 'client', 'service_staff', 'service_grade', 'service', 'staff', 'grade')",
            name=op.f("ck_rate_rules_level"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_rate_rules_period"),
        ),
        sa.CheckConstraint("rate >= 0", name=op.f("ck_rate_rules_rate")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_rate_rules_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_rate_rules_organization_id_client_id_clients",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "grade_id"],
            ["grades.organization_id", "grades.id"],
            name="fk_rate_rules_organization_id_grade_id_grades",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_rate_rules_organization_id_service_id_services",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_rate_rules_organization_id_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_rate_rules_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rate_rules")),
        sa.UniqueConstraint("organization_id", "id", name="uq_rate_rules_organization_id_id"),
    )
    op.create_index(
        "ix_rate_rules_organization_id_client_id",
        "rate_rules",
        ["organization_id", "client_id"],
        unique=False,
    )
    op.create_index(
        "ix_rate_rules_organization_id_grade_id",
        "rate_rules",
        ["organization_id", "grade_id"],
        unique=False,
    )
    op.create_index(
        "ix_rate_rules_organization_id_service_id",
        "rate_rules",
        ["organization_id", "service_id"],
        unique=False,
    )
    op.create_index(
        "ix_rate_rules_organization_id_user_id",
        "rate_rules",
        ["organization_id", "user_id"],
        unique=False,
    )
    op.create_table(
        "recurring_schedules",
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("frequency", sa.String(length=32), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("next_run_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("last_run_on", sa.Date(), nullable=True),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "frequency IN ('weekly', 'monthly', 'quarterly', 'annually')",
            name=op.f("ck_recurring_schedules_frequency"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'ended')", name=op.f("ck_recurring_schedules_status")
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 200", name=op.f("ck_recurring_schedules_name_length")
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR end_date >= start_date",
            name=op.f("ck_recurring_schedules_end_after_start"),
        ),
        sa.CheckConstraint(
            "next_run_date >= start_date", name=op.f("ck_recurring_schedules_next_after_start")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_recurring_schedules_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_recurring_schedules_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_recurring_schedules_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recurring_schedules")),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_recurring_schedules_organization_id_id"
        ),
    )
    op.create_index(
        "ix_recurring_schedules_due", "recurring_schedules", ["next_run_date"], unique=False
    )
    op.create_index(
        "ix_recurring_schedules_organization_id_client_id",
        "recurring_schedules",
        ["organization_id", "client_id"],
        unique=False,
    )
    op.create_table(
        "statement_sends",
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("date_from", sa.Date(), nullable=True),
        sa.Column("date_to", sa.Date(), nullable=False),
        sa.Column("recipients", sa.JSON(), nullable=False),
        sa.Column("closing_balance", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column(
            "sent_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("sent_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'sent') = (sent_at IS NOT NULL)", name=op.f("ck_statement_sends_sent_at")
        ),
        sa.CheckConstraint(
            "JSON_TYPE(recipients) = 'ARRAY' AND JSON_LENGTH(recipients) >= 1",
            name=op.f("ck_statement_sends_has_recipients"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'sent', 'failed')", name=op.f("ck_statement_sends_status")
        ),
        sa.CheckConstraint(
            "date_from IS NULL OR date_from <= date_to", name=op.f("ck_statement_sends_period")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_statement_sends_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_statement_sends_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sent_by"], ["users.id"], name=op.f("fk_statement_sends_sent_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statement_sends")),
        sa.UniqueConstraint("organization_id", "id", name="uq_statement_sends_organization_id_id"),
    )
    op.create_index(
        "ix_statement_sends_organization_id_client_id_created_at",
        "statement_sends",
        ["organization_id", "client_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "ticket_comments",
        sa.Column(
            "ticket_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "author_user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("body", sa.Text(), server_default=sa.text("('')"), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("edited_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "body_html IS NULL OR char_length(body_html) <= 20000",
            name=op.f("ck_ticket_comments_body_html_length"),
        ),
        sa.CheckConstraint(
            "char_length(body) <= 5000", name=op.f("ck_ticket_comments_body_length")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "author_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_ticket_comments_organization_id_author_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "ticket_id"],
            ["tickets.organization_id", "tickets.id"],
            name="fk_ticket_comments_ticket_id_tickets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_ticket_comments_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_comments")),
        sa.UniqueConstraint("organization_id", "id", name="uq_ticket_comments_organization_id_id"),
        sa.UniqueConstraint(
            "organization_id",
            "ticket_id",
            "id",
            name="uq_ticket_comments_organization_id_ticket_id_id",
        ),
    )
    op.create_index(
        "ix_ticket_comments_organization_id_ticket_id_created_at",
        "ticket_comments",
        ["organization_id", "ticket_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "ticket_status_history",
        sa.Column(
            "ticket_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column(
            "changed_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "changed_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status IN ('open', 'in_progress', 'requirement_pending', 'clarification_required', 'blocked', 'ready_for_ba_review', 'reopened', 'closed', 'invalid', 'on_hold')",
            name=op.f("ck_ticket_status_history_from_status"),
        ),
        sa.CheckConstraint(
            "to_status IN ('open', 'in_progress', 'requirement_pending', 'clarification_required', 'blocked', 'ready_for_ba_review', 'reopened', 'closed', 'invalid', 'on_hold')",
            name=op.f("ck_ticket_status_history_to_status"),
        ),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status <> to_status",
            name=op.f("ck_ticket_status_history_status_changed"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "changed_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_ticket_status_history_organization_id_changed_by_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "ticket_id"],
            ["tickets.organization_id", "tickets.id"],
            name="fk_ticket_status_history_ticket_id_tickets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_ticket_status_history_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_status_history")),
    )
    op.create_index(
        "ix_ticket_status_history_organization_id_ticket_id_at",
        "ticket_status_history",
        ["organization_id", "ticket_id", "changed_at"],
        unique=False,
    )
    op.create_table(
        "ticket_tag_links",
        sa.Column(
            "ticket_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("tag_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "tag_id"],
            ["ticket_tags.organization_id", "ticket_tags.id"],
            name="fk_ticket_tag_links_tag_id_ticket_tags",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "ticket_id"],
            ["tickets.organization_id", "tickets.id"],
            name="fk_ticket_tag_links_ticket_id_tickets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_ticket_tag_links_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("ticket_id", "tag_id", name=op.f("pk_ticket_tag_links")),
    )
    op.create_index(
        "ix_ticket_tag_links_organization_id_tag_id",
        "ticket_tag_links",
        ["organization_id", "tag_id"],
        unique=False,
    )
    op.create_table(
        "credit_notes",
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "invoice_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("number", sa.String(length=255), nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("net_total", sa.BigInteger(), nullable=False),
        sa.Column("vat_total", sa.BigInteger(), nullable=False),
        sa.Column("gross_total", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 500", name=op.f("ck_credit_notes_reason_length")
        ),
        sa.CheckConstraint(
            "gross_total = net_total + vat_total", name=op.f("ck_credit_notes_gross_total")
        ),
        sa.CheckConstraint("gross_total > 0", name=op.f("ck_credit_notes_gross_positive")),
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
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("incurred_on", sa.Date(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("supplier", sa.Text(), nullable=True),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "vat_rate_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("is_billable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "billing_status", sa.String(length=32), server_default="unbilled", nullable=False
        ),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
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
            "char_length(description) BETWEEN 1 AND 500",
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
        "ix_disbursements_wip", "disbursements", ["organization_id", "client_id"], unique=False
    )
    op.create_table(
        "engagement_job_runs",
        sa.Column(
            "client_service_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('created', 'failed')", name=op.f("ck_engagement_job_runs_status")
        ),
        sa.ForeignKeyConstraint(
            ["client_service_id"],
            ["client_services.id"],
            name=op.f("fk_engagement_job_runs_client_service_id_client_services"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f("fk_engagement_job_runs_job_id_jobs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_engagement_job_runs_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_engagement_job_runs")),
        sa.UniqueConstraint(
            "client_service_id", "period_start", name="uq_engagement_job_runs_period"
        ),
    )
    op.create_index(
        "ix_engagement_job_runs_organization_id_client_service_id",
        "engagement_job_runs",
        ["organization_id", "client_service_id"],
        unique=False,
    )
    op.create_table(
        "estimates",
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("number", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="draft", nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("net_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("vat_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("gross_total", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("sent_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("responded_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "converted_invoice_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=True,
        ),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'draft') = (number IS NULL)", name=op.f("ck_estimates_number_when_sent")
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(currency, '^[A-Z]{3}$', 'c')", name=op.f("ck_estimates_currency_format")
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'sent', 'accepted', 'declined')", name=op.f("ck_estimates_status")
        ),
        sa.CheckConstraint(
            "expiry_date IS NULL OR issue_date IS NULL OR expiry_date >= issue_date",
            name=op.f("ck_estimates_expiry_after_issue"),
        ),
        sa.CheckConstraint(
            "gross_total = net_total + vat_total", name=op.f("ck_estimates_gross_total")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_estimates_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_estimates_organization_id_client_id_job_id_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_estimates_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "converted_invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name="fk_estimates_organization_id_converted_invoice_id_invoices",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_estimates_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_estimates")),
        sa.UniqueConstraint("organization_id", "id", name="uq_estimates_organization_id_id"),
    )
    op.create_index(
        "ix_estimates_organization_id_client_id",
        "estimates",
        ["organization_id", "client_id"],
        unique=False,
    )
    op.create_index(
        "ix_estimates_organization_id_status",
        "estimates",
        ["organization_id", "status"],
        unique=False,
    )
    op.create_index(
        "uq_estimates_organization_id_number",
        "estimates",
        [
            sa.literal_column("(CASE WHEN number IS NOT NULL THEN `organization_id` END)"),
            sa.literal_column("(CASE WHEN number IS NOT NULL THEN `number` END)"),
        ],
        unique=True,
    )
    op.create_table(
        "expenses",
        sa.Column(
            "claim_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "expense_type_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column("incurred_on", sa.Date(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("supplier", sa.String(length=200), nullable=True),
        sa.Column(
            "vat_rate_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("vat_rate_bp", sa.Integer(), nullable=True),
        sa.Column("net", sa.BigInteger(), nullable=False),
        sa.Column("vat", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("gross", sa.BigInteger(), nullable=False),
        sa.Column("miles", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("mileage_rate_applied", sa.BigInteger(), nullable=True),
        sa.Column("is_billable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("markup_bp", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("recharge_amount", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("payer", sa.String(length=255), server_default="staff", nullable=False),
        sa.Column(
            "billing_status", sa.String(length=255), server_default="unbilled", nullable=False
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "billing_status IN ('unbilled', 'in_draft', 'billed', 'written_off')",
            name=op.f("ck_expenses_billing_status"),
        ),
        sa.CheckConstraint(
            "is_billable OR billing_status = 'unbilled'",
            name=op.f("ck_expenses_non_billable_status"),
        ),
        sa.CheckConstraint("payer IN ('staff', 'practice')", name=op.f("ck_expenses_payer")),
        sa.CheckConstraint(
            "(miles IS NULL) = (mileage_rate_applied IS NULL) AND (mileage_rate_applied IS NULL OR mileage_rate_applied >= 0)",
            name=op.f("ck_expenses_mileage_snapshot"),
        ),
        sa.CheckConstraint(
            "(vat_rate_id IS NULL) = (vat_rate_bp IS NULL)", name=op.f("ck_expenses_vat_snapshot")
        ),
        sa.CheckConstraint(
            "NOT is_billable OR client_id IS NOT NULL",
            name=op.f("ck_expenses_billable_needs_client"),
        ),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 500",
            name=op.f("ck_expenses_description_length"),
        ),
        sa.CheckConstraint("gross = net + vat", name=op.f("ck_expenses_gross_total")),
        sa.CheckConstraint(
            "is_billable OR recharge_amount = 0", name=op.f("ck_expenses_recharge_needs_billable")
        ),
        sa.CheckConstraint(
            "job_id IS NULL OR client_id IS NOT NULL", name=op.f("ck_expenses_job_needs_client")
        ),
        sa.CheckConstraint("markup_bp BETWEEN 0 AND 100000", name=op.f("ck_expenses_markup_bp")),
        sa.CheckConstraint("miles IS NULL OR miles > 0", name=op.f("ck_expenses_miles")),
        sa.CheckConstraint("net >= 0 AND vat >= 0", name=op.f("ck_expenses_amounts")),
        sa.CheckConstraint("recharge_amount >= 0", name=op.f("ck_expenses_recharge_amount")),
        sa.CheckConstraint(
            "supplier IS NULL OR char_length(supplier) BETWEEN 1 AND 200",
            name=op.f("ck_expenses_supplier_length"),
        ),
        sa.CheckConstraint(
            "vat_rate_bp IS NULL OR vat_rate_bp BETWEEN 0 AND 10000",
            name=op.f("ck_expenses_vat_rate_bp"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "claim_id"],
            ["expense_claims.organization_id", "expense_claims.id"],
            name="fk_expenses_organization_id_claim_id_expense_claims",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_expenses_organization_id_client_id_job_id_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_expenses_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "expense_type_id"],
            ["expense_types.organization_id", "expense_types.id"],
            name="fk_expenses_organization_id_expense_type_id_expense_types",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_expenses_organization_id_vat_rate_id_vat_rates",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_expenses_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expenses")),
        sa.UniqueConstraint("organization_id", "id", name="uq_expenses_organization_id_id"),
    )
    op.create_index(
        "ix_expenses_organization_id_claim_id",
        "expenses",
        ["organization_id", "claim_id"],
        unique=False,
    )
    op.create_index(
        "ix_expenses_organization_id_client_id_incurred_on",
        "expenses",
        ["organization_id", "client_id", "incurred_on"],
        unique=False,
    )
    op.create_index(
        "ix_expenses_organization_id_expense_type_id",
        "expenses",
        ["organization_id", "expense_type_id"],
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
    )
    op.create_index(
        "ix_expenses_wip", "expenses", ["organization_id", "client_id", "incurred_on"], unique=False
    )
    op.create_table(
        "invoice_approvals",
        sa.Column(
            "invoice_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column(
            "actor_user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("gross_total", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action <> 'rejected' OR note IS NOT NULL",
            name=op.f("ck_invoice_approvals_rejection_has_reason"),
        ),
        sa.CheckConstraint(
            "action IN ('submitted', 'approved', 'rejected')",
            name=op.f("ck_invoice_approvals_action"),
        ),
        sa.CheckConstraint("gross_total >= 0", name=op.f("ck_invoice_approvals_gross_total")),
        sa.CheckConstraint(
            "note IS NULL OR char_length(note) BETWEEN 1 AND 500",
            name=op.f("ck_invoice_approvals_note_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_invoice_approvals_organization_id_actor_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name="fk_invoice_approvals_invoice",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_invoice_approvals_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_approvals")),
    )
    op.create_index(
        "ix_invoice_approvals_invoice_id_created_at",
        "invoice_approvals",
        ["invoice_id", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "invoice_lines",
        sa.Column(
            "invoice_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=False),
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column(
            "service_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "quantity",
            sa.Numeric(precision=12, scale=2),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("unit_amount", sa.BigInteger(), nullable=False),
        sa.Column("net_amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "vat_rate_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("vat_rate_bp", sa.Integer(), nullable=False),
        sa.Column("vat_amount", sa.BigInteger(), nullable=False),
        sa.Column("is_advance", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "NOT is_advance OR kind = 'manual'", name=op.f("ck_invoice_lines_advance_is_manual")
        ),
        sa.CheckConstraint(
            "kind IN ('time', 'fixed_fee', 'disbursement', 'manual')",
            name=op.f("ck_invoice_lines_kind"),
        ),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 1000",
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
        "job_assignees",
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
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
        "job_budget_alerts",
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("threshold", sa.SmallInteger(), nullable=False),
        sa.Column(
            "triggered_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("usage_bp", sa.BigInteger(), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('minutes', 'fee')", name=op.f("ck_job_budget_alerts_kind")),
        sa.CheckConstraint("threshold IN (80, 100)", name=op.f("ck_job_budget_alerts_threshold")),
        sa.CheckConstraint("usage_bp >= 0", name=op.f("ck_job_budget_alerts_usage_bp")),
        sa.ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            name="fk_job_budget_alerts_organization_id_job_id_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_job_budget_alerts_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_budget_alerts")),
        sa.UniqueConstraint(
            "organization_id",
            "job_id",
            "kind",
            "threshold",
            name="uq_job_budget_alerts_organization_id_job_id_kind_threshold",
        ),
    )
    op.create_table(
        "payment_allocations",
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "payment_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "invoice_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
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
        "payment_reminders",
        sa.Column(
            "invoice_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "batch_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("template", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("sent_on", sa.Date(), nullable=False),
        sa.Column("recipients", sa.JSON(), nullable=False),
        sa.Column("days_overdue", sa.Integer(), nullable=False),
        sa.Column("amount_due", sa.BigInteger(), nullable=False),
        sa.Column("sent_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("sent_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(source = 'scheduled') = (sent_by IS NULL)", name=op.f("ck_payment_reminders_sender")
        ),
        sa.CheckConstraint(
            "(status = 'sent') = (sent_at IS NOT NULL)", name=op.f("ck_payment_reminders_sent_at")
        ),
        sa.CheckConstraint(
            "JSON_TYPE(recipients) = 'ARRAY' AND JSON_LENGTH(recipients) >= 1",
            name=op.f("ck_payment_reminders_has_recipients"),
        ),
        sa.CheckConstraint(
            "source IN ('manual', 'scheduled')", name=op.f("ck_payment_reminders_source")
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'sent', 'failed', 'skipped')",
            name=op.f("ck_payment_reminders_status"),
        ),
        sa.CheckConstraint(
            "char_length(template) BETWEEN 1 AND 80",
            name=op.f("ck_payment_reminders_template_length"),
        ),
        sa.CheckConstraint(
            "days_overdue >= 0 AND amount_due >= 0", name=op.f("ck_payment_reminders_figures")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name="fk_payment_reminders_invoice",
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
            "organization_id", "id", name="uq_payment_reminders_organization_id_id"
        ),
    )
    op.create_index(
        "ix_payment_reminders_batch_id", "payment_reminders", ["batch_id"], unique=False
    )
    op.create_index(
        "ix_payment_reminders_invoice_id", "payment_reminders", ["invoice_id"], unique=False
    )
    op.create_index(
        "ix_payment_reminders_organization_id_created_at",
        "payment_reminders",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "uq_payment_reminders_organization_id_invoice_id_sent_on",
        "payment_reminders",
        [
            sa.literal_column(
                "(CASE WHEN status IN ('queued', 'sent') THEN `organization_id` END)"
            ),
            sa.literal_column("(CASE WHEN status IN ('queued', 'sent') THEN `invoice_id` END)"),
            sa.literal_column("(CASE WHEN status IN ('queued', 'sent') THEN `sent_on` END)"),
        ],
        unique=True,
    )
    op.create_table(
        "recurring_lines",
        sa.Column(
            "schedule_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=False),
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column(
            "service_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "quantity",
            sa.Numeric(precision=12, scale=2),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("unit_amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "vat_rate_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 1000",
            name=op.f("ck_recurring_lines_description_length"),
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_recurring_lines_quantity")),
        sa.ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            name="fk_recurring_lines_organization_id_job_id_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["recurring_schedules.organization_id", "recurring_schedules.id"],
            name="fk_recurring_lines_schedule",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_recurring_lines_organization_id_service_id_services",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_recurring_lines_organization_id_vat_rate_id_vat_rates",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_recurring_lines_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recurring_lines")),
    )
    op.create_index(
        "ix_recurring_lines_schedule_id_position",
        "recurring_lines",
        ["schedule_id", "position"],
        unique=False,
    )
    op.create_table(
        "recurring_runs",
        sa.Column(
            "schedule_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "invoice_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "ran_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'succeeded') = (invoice_id IS NOT NULL)",
            name=op.f("ck_recurring_runs_invoice_when_succeeded"),
        ),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed')", name=op.f("ck_recurring_runs_status")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name="fk_recurring_runs_organization_id_invoice_id_invoices",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "schedule_id"],
            ["recurring_schedules.organization_id", "recurring_schedules.id"],
            name="fk_recurring_runs_schedule",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_recurring_runs_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recurring_runs")),
        sa.UniqueConstraint(
            "schedule_id", "run_date", name="uq_recurring_runs_schedule_id_run_date"
        ),
    )
    op.create_index(
        "ix_recurring_runs_schedule_id_run_date",
        "recurring_runs",
        ["schedule_id", "run_date"],
        unique=False,
    )
    op.create_table(
        "tasks",
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), server_default="not_started", nullable=False),
        sa.Column("budget_minutes", sa.Integer(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("pm_task_id", sa.String(length=64), nullable=True),
        sa.Column("pm_linked_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)",
            name=op.f("ck_tasks_completed_at"),
        ),
        sa.CheckConstraint(
            "status IN ('not_started', 'in_progress', 'on_hold', 'completed')",
            name=op.f("ck_tasks_status"),
        ),
        sa.CheckConstraint(
            "(pm_task_id IS NULL) = (pm_linked_at IS NULL)", name=op.f("ck_tasks_pm_link")
        ),
        sa.CheckConstraint(
            "budget_minutes IS NULL OR budget_minutes >= 0", name=op.f("ck_tasks_budget_minutes")
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 200", name=op.f("ck_tasks_title_length")
        ),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) <= 2000",
            name=op.f("ck_tasks_description_length"),
        ),
        sa.CheckConstraint(
            "pm_task_id IS NULL OR char_length(pm_task_id) BETWEEN 1 AND 64",
            name=op.f("ck_tasks_pm_task_id_length"),
        ),
        sa.CheckConstraint("sort_order >= 0", name=op.f("ck_tasks_sort_order")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_tasks_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            name="fk_tasks_organization_id_job_id_jobs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_tasks_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
        sa.UniqueConstraint("organization_id", "id", name="uq_tasks_organization_id_id"),
        sa.UniqueConstraint(
            "organization_id", "job_id", "id", name="uq_tasks_organization_id_job_id_id"
        ),
    )
    op.create_index(
        "ix_tasks_organization_id_due_date", "tasks", ["organization_id", "due_date"], unique=False
    )
    op.create_index(
        "ix_tasks_organization_id_job_id_sort_order",
        "tasks",
        ["organization_id", "job_id", "sort_order"],
        unique=False,
    )
    op.create_index(
        "ix_tasks_organization_id_status_due_date",
        "tasks",
        ["organization_id", "status", "due_date"],
        unique=False,
    )
    op.create_index(
        "uq_tasks_organization_id_pm_task_id",
        "tasks",
        [
            sa.literal_column("(CASE WHEN pm_task_id IS NOT NULL THEN `organization_id` END)"),
            sa.literal_column("(CASE WHEN pm_task_id IS NOT NULL THEN `pm_task_id` END)"),
        ],
        unique=True,
    )
    op.create_table(
        "ticket_attachments",
        sa.Column(
            "ticket_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "comment_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("file_name", sa.String(length=200), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=128), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "uploaded_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(sha256, '^[0-9a-f]{64}$', 'c')",
            name=op.f("ck_ticket_attachments_sha256_format"),
        ),
        sa.CheckConstraint(
            "char_length(content_type) BETWEEN 3 AND 100",
            name=op.f("ck_ticket_attachments_content_type_length"),
        ),
        sa.CheckConstraint(
            "char_length(file_name) BETWEEN 1 AND 200",
            name=op.f("ck_ticket_attachments_file_name_length"),
        ),
        sa.CheckConstraint(
            "char_length(storage_key) BETWEEN 16 AND 128",
            name=op.f("ck_ticket_attachments_storage_key_length"),
        ),
        sa.CheckConstraint(
            "size_bytes BETWEEN 1 AND 2097152", name=op.f("ck_ticket_attachments_size_bytes")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "ticket_id", "comment_id"],
            ["ticket_comments.organization_id", "ticket_comments.ticket_id", "ticket_comments.id"],
            name="fk_ticket_attachments_comment_id_ticket_comments",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "ticket_id"],
            ["tickets.organization_id", "tickets.id"],
            name="fk_ticket_attachments_ticket_id_tickets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "uploaded_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_ticket_attachments_organization_id_uploader_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_ticket_attachments_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_attachments")),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_ticket_attachments_organization_id_id"
        ),
    )
    op.create_index(
        "ix_ticket_attachments_organization_id_comment_id",
        "ticket_attachments",
        ["organization_id", "comment_id"],
        unique=False,
    )
    op.create_index(
        "ix_ticket_attachments_organization_id_ticket_id",
        "ticket_attachments",
        ["organization_id", "ticket_id"],
        unique=False,
    )
    op.create_index(
        "uq_ticket_attachments_storage_key", "ticket_attachments", ["storage_key"], unique=True
    )
    op.create_table(
        "wip_adjustments",
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=255), server_default="pending", nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False),
        sa.Column("threshold", sa.BigInteger(), nullable=False),
        sa.Column(
            "requested_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "decided_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("decided_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column(
            "movement_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "reversal_movement_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=True,
        ),
        sa.Column(
            "reversed_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("reversed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("reversal_reason", sa.Text(), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND movement_id IS NULL AND decided_by IS NULL AND reversed_by IS NULL) OR (status = 'approved' AND movement_id IS NOT NULL AND reversed_by IS NULL AND (decided_by IS NOT NULL) = approval_required) OR (status = 'rejected' AND movement_id IS NULL AND decided_by IS NOT NULL AND decision_note IS NOT NULL AND reversed_by IS NULL) OR (status = 'reversed' AND movement_id IS NOT NULL AND reversal_movement_id IS NOT NULL AND reversed_by IS NOT NULL AND reversal_reason IS NOT NULL)",
            name=op.f("ck_wip_adjustments_status_fields"),
        ),
        sa.CheckConstraint(
            "component IN ('time', 'fixed_fee', 'expense')",
            name=op.f("ck_wip_adjustments_component"),
        ),
        sa.CheckConstraint(
            "kind IN ('write_on', 'write_off')", name=op.f("ck_wip_adjustments_kind")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'reversed')",
            name=op.f("ck_wip_adjustments_status"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_wip_adjustments_amount")),
        sa.CheckConstraint(
            "char_length(trim(reason)) BETWEEN 1 AND 500",
            name=op.f("ck_wip_adjustments_reason_length"),
        ),
        sa.CheckConstraint(
            "decision_note IS NULL OR char_length(trim(decision_note)) BETWEEN 1 AND 500",
            name=op.f("ck_wip_adjustments_decision_note_length"),
        ),
        sa.CheckConstraint(
            "reversal_reason IS NULL OR char_length(trim(reversal_reason)) BETWEEN 1 AND 500",
            name=op.f("ck_wip_adjustments_reversal_reason_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_wip_adjustments_organization_id_client_id_job_id_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_wip_adjustments_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "decided_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_wip_adjustments_organization_id_decided_by_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "requested_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_wip_adjustments_organization_id_requested_by_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "reversed_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_wip_adjustments_organization_id_reversed_by_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_wip_adjustments_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wip_adjustments")),
        sa.UniqueConstraint("organization_id", "id", name="uq_wip_adjustments_organization_id_id"),
    )
    op.create_index(
        "ix_wip_adjustments_organization_id_client_id",
        "wip_adjustments",
        ["organization_id", "client_id"],
        unique=False,
    )
    op.create_index(
        "ix_wip_adjustments_organization_id_created_at",
        "wip_adjustments",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_wip_adjustments_pending",
        "wip_adjustments",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "wip_transfers",
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "from_job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "to_job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("items", sa.JSON(), server_default=sa.text("(JSON_ARRAY())"), nullable=False),
        sa.Column("status", sa.String(length=255), server_default="applied", nullable=False),
        sa.Column(
            "out_movement_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "in_movement_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "reversed_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("reversed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("reversal_reason", sa.Text(), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'applied' AND reversed_by IS NULL AND reversed_at IS NULL AND reversal_reason IS NULL) OR (status = 'reversed' AND reversed_by IS NOT NULL AND reversed_at IS NOT NULL AND reversal_reason IS NOT NULL)",
            name=op.f("ck_wip_transfers_status_fields"),
        ),
        sa.CheckConstraint("JSON_TYPE(items) = 'ARRAY'", name=op.f("ck_wip_transfers_items_array")),
        sa.CheckConstraint(
            "component IN ('time', 'fixed_fee', 'expense')", name=op.f("ck_wip_transfers_component")
        ),
        sa.CheckConstraint(
            "status IN ('applied', 'reversed')", name=op.f("ck_wip_transfers_status")
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_wip_transfers_amount")),
        sa.CheckConstraint(
            "char_length(trim(reason)) BETWEEN 1 AND 500",
            name=op.f("ck_wip_transfers_reason_length"),
        ),
        sa.CheckConstraint(
            "from_job_id <> to_job_id", name=op.f("ck_wip_transfers_different_jobs")
        ),
        sa.CheckConstraint(
            "reversal_reason IS NULL OR char_length(trim(reversal_reason)) BETWEEN 1 AND 500",
            name=op.f("ck_wip_transfers_reversal_reason_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "from_job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_wip_transfers_organization_id_client_id_from_job_id_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id", "to_job_id"],
            ["jobs.organization_id", "jobs.client_id", "jobs.id"],
            name="fk_wip_transfers_organization_id_client_id_to_job_id_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_wip_transfers_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_wip_transfers_organization_id_created_by_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "reversed_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_wip_transfers_organization_id_reversed_by_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_wip_transfers_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wip_transfers")),
        sa.UniqueConstraint("organization_id", "id", name="uq_wip_transfers_organization_id_id"),
    )
    op.create_index(
        "ix_wip_transfers_organization_id_client_id",
        "wip_transfers",
        ["organization_id", "client_id"],
        unique=False,
    )
    op.create_index(
        "ix_wip_transfers_organization_id_created_at",
        "wip_transfers",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_table(
        "wip_write_offs",
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("written_off_on", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("total_wip_value", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 500", name=op.f("ck_wip_write_offs_reason_length")
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
        sa.Column(
            "credit_note_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=False),
        sa.Column("net_amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "vat_rate_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("vat_rate_bp", sa.Integer(), nullable=False),
        sa.Column("vat_amount", sa.BigInteger(), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 1000",
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
        "credit_note_wip",
        sa.Column(
            "credit_note_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "invoice_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("net_credited", sa.BigInteger(), nullable=False),
        sa.Column("wip_value", sa.BigInteger(), nullable=False),
        sa.Column("item_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint("mode IN ('return', 'write_off')", name=op.f("ck_credit_note_wip_mode")),
        sa.CheckConstraint(
            "char_length(trim(reason)) BETWEEN 1 AND 500",
            name=op.f("ck_credit_note_wip_reason_length"),
        ),
        sa.CheckConstraint(
            "net_credited > 0 AND wip_value >= 0 AND item_count >= 0",
            name=op.f("ck_credit_note_wip_values"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_credit_note_wip_organization_id_client_id_clients",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_credit_note_wip_organization_id_created_by_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "credit_note_id"],
            ["credit_notes.organization_id", "credit_notes.id"],
            name="fk_credit_note_wip_organization_id_credit_note_id_credit_notes",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_credit_note_wip_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_credit_note_wip")),
        sa.UniqueConstraint(
            "organization_id",
            "credit_note_id",
            name="uq_credit_note_wip_organization_id_credit_note_id",
        ),
    )
    op.create_table(
        "estimate_lines",
        sa.Column(
            "estimate_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=False),
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column(
            "service_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "quantity",
            sa.Numeric(precision=12, scale=2),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("unit_amount", sa.BigInteger(), nullable=False),
        sa.Column("net_amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "vat_rate_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("vat_rate_bp", sa.Integer(), nullable=False),
        sa.Column("vat_amount", sa.BigInteger(), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 1000",
            name=op.f("ck_estimate_lines_description_length"),
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_estimate_lines_quantity")),
        sa.CheckConstraint(
            "vat_rate_bp BETWEEN 0 AND 10000", name=op.f("ck_estimate_lines_vat_rate_bp")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "estimate_id"],
            ["estimates.organization_id", "estimates.id"],
            name="fk_estimate_lines_organization_id_estimate_id_estimates",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            name="fk_estimate_lines_organization_id_job_id_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_estimate_lines_organization_id_service_id_services",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_estimate_lines_organization_id_vat_rate_id_vat_rates",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_estimate_lines_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_estimate_lines")),
    )
    op.create_index(
        "ix_estimate_lines_estimate_id_position",
        "estimate_lines",
        ["estimate_id", "position"],
        unique=False,
    )
    op.create_table(
        "expense_receipts",
        sa.Column(
            "expense_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("file_name", sa.String(length=200), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=128), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "uploaded_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "REGEXP_LIKE(sha256, '^[0-9a-f]{64}$', 'c')",
            name=op.f("ck_expense_receipts_sha256_format"),
        ),
        sa.CheckConstraint(
            "char_length(content_type) BETWEEN 3 AND 100",
            name=op.f("ck_expense_receipts_content_type_length"),
        ),
        sa.CheckConstraint(
            "char_length(file_name) BETWEEN 1 AND 200",
            name=op.f("ck_expense_receipts_file_name_length"),
        ),
        sa.CheckConstraint(
            "char_length(storage_key) BETWEEN 16 AND 128",
            name=op.f("ck_expense_receipts_storage_key_length"),
        ),
        sa.CheckConstraint(
            "size_bytes BETWEEN 1 AND 10485760", name=op.f("ck_expense_receipts_size_bytes")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "expense_id"],
            ["expenses.organization_id", "expenses.id"],
            name="fk_expense_receipts_expense_id_expenses",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "uploaded_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_expense_receipts_organization_id_uploader_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_expense_receipts_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expense_receipts")),
        sa.UniqueConstraint("organization_id", "id", name="uq_expense_receipts_organization_id_id"),
    )
    op.create_index(
        "ix_expense_receipts_organization_id_expense_id",
        "expense_receipts",
        ["organization_id", "expense_id"],
        unique=False,
    )
    op.create_index(
        "uq_expense_receipts_storage_key", "expense_receipts", ["storage_key"], unique=True
    )
    op.create_table(
        "task_assignees",
        sa.Column(
            "task_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_task_assignees_organization_id_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_task_assignees_organization_id_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_task_assignees_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("task_id", "user_id", name=op.f("pk_task_assignees")),
    )
    op.create_index(
        "ix_task_assignees_organization_id_user_id",
        "task_assignees",
        ["organization_id", "user_id"],
        unique=False,
    )
    op.create_table(
        "time_entries",
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "timesheet_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("task_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column(
            "activity_code_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=True,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("internal_note", sa.Text(), nullable=True),
        sa.Column("is_billable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_overtime", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("charge_rate", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_rate", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("charge_value", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_value", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("rate_source", sa.String(length=32), nullable=True),
        sa.Column(
            "billing_status", sa.String(length=32), server_default="unbilled", nullable=False
        ),
        sa.Column(
            "created_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "is_opening_balance", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
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
            "rate_source IS NULL OR rate_source IN ('client_service_staff', 'client_service', 'client_staff', 'client', 'job', 'service_staff', 'service_grade', 'service', 'staff', 'grade', 'default', 'none')",
            name=op.f("ck_time_entries_rate_source"),
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
            "internal_note IS NULL OR char_length(internal_note) <= 2000",
            name=op.f("ck_time_entries_internal_note_length"),
        ),
        sa.CheckConstraint(
            "job_id IS NULL OR client_id IS NOT NULL", name=op.f("ck_time_entries_job_needs_client")
        ),
        sa.CheckConstraint("minutes BETWEEN 1 AND 1440", name=op.f("ck_time_entries_minutes")),
        sa.CheckConstraint(
            "task_id IS NULL OR job_id IS NOT NULL", name=op.f("ck_time_entries_task_needs_job")
        ),
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
            ["organization_id", "job_id", "task_id"],
            ["tasks.organization_id", "tasks.job_id", "tasks.id"],
            name="fk_time_entries_organization_id_job_id_task_id_tasks",
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
        "ix_time_entries_organization_id_task_id",
        "time_entries",
        ["organization_id", "task_id"],
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
    )
    op.create_table(
        "timers",
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "client_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("job_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column("task_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True),
        sa.Column(
            "activity_code_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=True,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_billable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("accumulated_seconds", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint("accumulated_seconds >= 0", name=op.f("ck_timers_accumulated_seconds")),
        sa.CheckConstraint(
            "job_id IS NULL OR client_id IS NOT NULL", name=op.f("ck_timers_job_needs_client")
        ),
        sa.CheckConstraint(
            "task_id IS NULL OR job_id IS NOT NULL", name=op.f("ck_timers_task_needs_job")
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
            ["organization_id", "job_id", "task_id"],
            ["tasks.organization_id", "tasks.job_id", "tasks.id"],
            name="fk_timers_organization_id_job_id_task_id_tasks",
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
    op.create_table(
        "time_entry_overtime",
        sa.Column(
            "time_entry_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column(
            "user_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False
        ),
        sa.Column("overtime_minutes", sa.Integer(), nullable=False),
        sa.Column("allocated", sa.Boolean(), nullable=False),
        sa.Column(
            "overtime_rate_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=True,
        ),
        sa.Column("rate_source", sa.String(length=32), nullable=False),
        sa.Column("overtime_rate", sa.BigInteger(), nullable=False),
        sa.Column("overtime_cost", sa.BigInteger(), nullable=False),
        sa.Column("premium_cost", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column(
            "confirmed_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("confirmed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "decided_by", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("decided_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "rate_source IN ('staff', 'grade', 'none')",
            name=op.f("ck_time_entry_overtime_rate_source"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name=op.f("ck_time_entry_overtime_status"),
        ),
        sa.CheckConstraint("overtime_cost >= 0", name=op.f("ck_time_entry_overtime_overtime_cost")),
        sa.CheckConstraint(
            "overtime_minutes > 0", name=op.f("ck_time_entry_overtime_overtime_minutes")
        ),
        sa.CheckConstraint("overtime_rate >= 0", name=op.f("ck_time_entry_overtime_overtime_rate")),
        sa.CheckConstraint("premium_cost >= 0", name=op.f("ck_time_entry_overtime_premium_cost")),
        sa.ForeignKeyConstraint(
            ["confirmed_by"], ["users.id"], name=op.f("fk_time_entry_overtime_confirmed_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"], name=op.f("fk_time_entry_overtime_decided_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "time_entry_id"],
            ["time_entries.organization_id", "time_entries.id"],
            name="fk_time_entry_overtime_time_entries",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_time_entry_overtime_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_time_entry_overtime_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["overtime_rate_id"],
            ["overtime_rates.id"],
            name=op.f("fk_time_entry_overtime_overtime_rate_id_overtime_rates"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_time_entry_overtime")),
        sa.UniqueConstraint("time_entry_id", name="uq_time_entry_overtime_time_entry_id"),
    )
    op.create_index(
        "ix_time_entry_overtime_organization_id_status",
        "time_entry_overtime",
        ["organization_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_time_entry_overtime_organization_id_user_id",
        "time_entry_overtime",
        ["organization_id", "user_id"],
        unique=False,
    )
    op.create_table(
        "wip_allocations",
        sa.Column(
            "time_entry_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "disbursement_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "expense_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "invoice_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "invoice_line_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column(
            "write_off_id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=True
        ),
        sa.Column("wip_value", sa.BigInteger(), nullable=False),
        sa.Column("billed_value", sa.BigInteger(), nullable=False),
        sa.Column("reversed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("assigned_on", sa.Date(), nullable=True),
        sa.Column("id", mysql.CHAR(36, charset="ascii", collation="ascii_bin"), nullable=False),
        sa.Column(
            "organization_id",
            mysql.CHAR(36, charset="ascii", collation="ascii_bin"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"),
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
            "(time_entry_id IS NOT NULL) + (disbursement_id IS NOT NULL) + (expense_id IS NOT NULL) = 1",
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
            ["organization_id", "expense_id"],
            ["expenses.organization_id", "expenses.id"],
            name="fk_wip_allocations_expense",
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
        [
            sa.literal_column(
                "(CASE WHEN disbursement_id IS NOT NULL AND reversed_at IS NULL THEN `disbursement_id` END)"
            )
        ],
        unique=True,
    )
    op.create_index(
        "uq_wip_allocations_expense_live",
        "wip_allocations",
        [
            sa.literal_column(
                "(CASE WHEN expense_id IS NOT NULL AND reversed_at IS NULL THEN `expense_id` END)"
            )
        ],
        unique=True,
    )
    op.create_index(
        "uq_wip_allocations_time_entry_live",
        "wip_allocations",
        [
            sa.literal_column(
                "(CASE WHEN time_entry_id IS NOT NULL AND reversed_at IS NULL THEN `time_entry_id` END)"
            )
        ],
        unique=True,
    )
    # ### end Alembic commands ###

    # The permission catalogue (app/core/permissions.py); practices copy roles from it.
    sync_permissions()

    # No overlapping periods and append-only history (app/db/triggers.py).
    for statement in _TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    # Every table, newest first; their indexes and triggers go with them. Foreign keys between
    # them are not checked meanwhile, so the order cannot fail half-way.
    op.execute("SET FOREIGN_KEY_CHECKS = 0")
    for table in reversed(_TABLES):
        op.drop_table(table)
    op.execute("SET FOREIGN_KEY_CHECKS = 1")
