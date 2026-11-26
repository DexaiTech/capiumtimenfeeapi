"""create accounts and practices

Revision ID: 002
Revises: 001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.migration_helpers import add_updated_at_triggers, sync_permissions

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGER_TABLES = (
    "background_jobs",
    "permissions",
    "users",
    "email_verification_tokens",
    "organizations",
    "password_reset_tokens",
    "user_sessions",
    "refresh_tokens",
    "roles",
    "organization_members",
)


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.UUID(), nullable=True),
        sa.Column("actor_user_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.UUID(), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", "created_at", name=op.f("pk_audit_logs")),
        postgresql_partition_by="RANGE (created_at)",
    )
    op.create_index(
        "ix_audit_logs_organization_id_actor_user_id_created_at",
        "audit_logs",
        ["organization_id", "actor_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_organization_id_created_at",
        "audit_logs",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "background_jobs",
        sa.Column("organization_id", sa.UUID(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("100"), nullable=False),
        sa.Column(
            "run_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
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
            "status IN ('queued', 'running', 'succeeded', 'failed', 'dead')",
            name=op.f("ck_background_jobs_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_background_jobs")),
    )
    op.create_index(
        "ix_background_jobs_queued",
        "background_jobs",
        ["priority", "run_at"],
        unique=False,
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        "ix_background_jobs_running_locked_until",
        "background_jobs",
        ["locked_until"],
        unique=False,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "uq_background_jobs_dedupe_key",
        "background_jobs",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL AND status IN ('queued', 'running')"),
    )
    op.create_table(
        "permissions",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permissions")),
        sa.UniqueConstraint("code", name="uq_permissions_code"),
    )
    op.create_table(
        "rate_limit_counters",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("key", "window_start", name=op.f("pk_rate_limit_counters")),
        prefixes=["UNLOGGED"],
    )
    op.create_table(
        "users",
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), server_default="Europe/London", nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "must_change_password", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
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
            "length(full_name) BETWEEN 1 AND 120", name=op.f("ck_users_full_name_length")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(
        "uq_users_email_lower",
        "users",
        [sa.literal_column("lower(email)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "email_verification_tokens",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
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
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_ip", postgresql.INET(), nullable=True),
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
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), server_default="Europe/London", nullable=False),
        sa.Column("week_start", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "working_days",
            postgresql.ARRAY(sa.SmallInteger()),
            server_default=sa.text("'{1,2,3,4,5}'"),
            nullable=False,
        ),
        sa.Column("minutes_per_day", sa.Integer(), server_default=sa.text("450"), nullable=False),
        sa.Column("currency", sa.Text(), server_default="GBP", nullable=False),
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
            "time_increment_minutes", sa.SmallInteger(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column(
            "require_timesheet_approval",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("address_line1", sa.Text(), nullable=True),
        sa.Column("address_line2", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("postcode", sa.Text(), nullable=True),
        sa.Column("country", sa.Text(), server_default="GB", nullable=False),
        sa.Column("bank_details", sa.Text(), nullable=True),
        sa.Column("invoice_footer", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
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
            "currency ~ '^[A-Z]{3}$'", name=op.f("ck_organizations_currency_format")
        ),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$'", name=op.f("ck_organizations_slug_format")
        ),
        sa.CheckConstraint(
            "default_charge_rate >= 0", name=op.f("ck_organizations_default_charge_rate")
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 120", name=op.f("ck_organizations_name_length")
        ),
        sa.CheckConstraint(
            "minutes_per_day BETWEEN 1 AND 1440", name=op.f("ck_organizations_minutes_per_day")
        ),
        sa.CheckConstraint(
            "payment_terms_days BETWEEN 0 AND 365", name=op.f("ck_organizations_payment_terms_days")
        ),
        sa.CheckConstraint(
            "time_increment_minutes IN (1, 5, 6, 10, 15, 30, 60)",
            name=op.f("ck_organizations_increment"),
        ),
        sa.CheckConstraint("week_start BETWEEN 1 AND 7", name=op.f("ck_organizations_week_start")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_organizations_created_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_organizations")),
    )
    op.create_index(
        "uq_organizations_slug",
        "organizations",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "password_reset_tokens",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
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
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_ip", postgresql.INET(), nullable=True),
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
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
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
    op.create_index(
        "ix_user_sessions_user_id",
        "user_sessions",
        ["user_id"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "refresh_tokens",
        sa.Column("session_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
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
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("system_key", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
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
        sa.CheckConstraint("length(name) BETWEEN 1 AND 60", name=op.f("ck_roles_name_length")),
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
        ["organization_id", sa.literal_column("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_roles_organization_id_system_key",
        "roles",
        ["organization_id", "system_key"],
        unique=True,
        postgresql_where=sa.text("system_key IS NOT NULL"),
    )
    op.create_table(
        "organization_members",
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("manager_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "weekly_capacity_minutes", sa.Integer(), server_default=sa.text("2250"), nullable=False
        ),
        sa.Column(
            "chargeable_target_bp", sa.Integer(), server_default=sa.text("7500"), nullable=False
        ),
        sa.Column("job_title", sa.Text(), nullable=True),
        sa.Column("initials", sa.Text(), nullable=True),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("id", sa.UUID(), nullable=False),
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
            "status IN ('active', 'suspended', 'removed')",
            name=op.f("ck_organization_members_status"),
        ),
        sa.CheckConstraint(
            "manager_user_id <> user_id", name=op.f("ck_organization_members_not_own_manager")
        ),
        sa.CheckConstraint(
            "weekly_capacity_minutes BETWEEN 0 AND 10080",
            name=op.f("ck_organization_members_weekly_capacity"),
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
        ["organization_id", sa.literal_column("upper(initials)")],
        unique=True,
        postgresql_where=sa.text("initials IS NOT NULL AND status <> 'removed'"),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.UUID(), nullable=False),
        sa.Column("permission_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
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
    op.execute("CREATE TABLE audit_logs_default PARTITION OF audit_logs DEFAULT")
    # Current month and three ahead; the worker keeps this window moving.
    op.execute(
        "SELECT create_monthly_partition('audit_logs', (date_trunc('month', now()) "
        "+ make_interval(months => m))::date) FROM generate_series(0, 3) AS m"
    )
    add_updated_at_triggers(*TRIGGER_TABLES)
    sync_permissions()


def downgrade() -> None:
    op.drop_table("role_permissions")
    op.drop_index(
        "uq_organization_members_organization_id_initials",
        table_name="organization_members",
        postgresql_where=sa.text("initials IS NOT NULL AND status <> 'removed'"),
    )
    op.drop_index("ix_organization_members_user_id", table_name="organization_members")
    op.drop_index(
        "ix_organization_members_organization_id_manager_user_id", table_name="organization_members"
    )
    op.drop_table("organization_members")
    op.drop_index(
        "uq_roles_organization_id_system_key",
        table_name="roles",
        postgresql_where=sa.text("system_key IS NOT NULL"),
    )
    op.drop_index(
        "uq_roles_organization_id_name_lower",
        table_name="roles",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_table("roles")
    op.drop_index("uq_refresh_tokens_token_hash", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_session_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index(
        "ix_user_sessions_user_id",
        table_name="user_sessions",
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.drop_table("user_sessions")
    op.drop_index("uq_password_reset_tokens_token_hash", table_name="password_reset_tokens")
    op.drop_index("ix_password_reset_tokens_user_id", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_index(
        "uq_organizations_slug",
        table_name="organizations",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_table("organizations")
    op.drop_index("uq_email_verification_tokens_token_hash", table_name="email_verification_tokens")
    op.drop_index("ix_email_verification_tokens_user_id", table_name="email_verification_tokens")
    op.drop_table("email_verification_tokens")
    op.drop_index(
        "uq_users_email_lower", table_name="users", postgresql_where=sa.text("deleted_at IS NULL")
    )
    op.drop_table("users")
    op.drop_table("rate_limit_counters")
    op.drop_table("permissions")
    op.drop_index(
        "uq_background_jobs_dedupe_key",
        table_name="background_jobs",
        postgresql_where=sa.text("dedupe_key IS NOT NULL AND status IN ('queued', 'running')"),
    )
    op.drop_index(
        "ix_background_jobs_running_locked_until",
        table_name="background_jobs",
        postgresql_where=sa.text("status = 'running'"),
    )
    op.drop_index(
        "ix_background_jobs_queued",
        table_name="background_jobs",
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.drop_table("background_jobs")
    op.drop_index("ix_audit_logs_organization_id_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_organization_id_actor_user_id_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
