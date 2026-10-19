"""create practice settings and clients

Revision ID: 003
Revises: 002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGER_TABLES = (
    "activity_codes",
    "document_sequences",
    "vat_rates",
    "services",
    "clients",
    "staff_rates",
    "client_contacts",
    "client_rates",
)


def upgrade() -> None:
    op.create_table(
        "activity_codes",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_chargeable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
            "code ~ '^[A-Z0-9_-]{1,12}$'", name=op.f("ck_activity_codes_code_format")
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 120", name=op.f("ck_activity_codes_name_length")
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
    op.create_table(
        "document_sequences",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), server_default="", nullable=False),
        sa.Column("next_number", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("padding", sa.SmallInteger(), server_default=sa.text("5"), nullable=False),
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
            "kind IN ('client', 'job', 'invoice', 'credit_note')",
            name=op.f("ck_document_sequences_kind"),
        ),
        sa.CheckConstraint(
            "prefix ~ '^[A-Za-z0-9/_-]{0,12}$'", name=op.f("ck_document_sequences_prefix_format")
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
        "vat_rates",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("rate_bp", sa.Integer(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
            "code IN ('standard', 'reduced', 'zero', 'exempt', 'outside_scope')",
            name=op.f("ck_vat_rates_code"),
        ),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 60", name=op.f("ck_vat_rates_name_length")),
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
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("is_default AND archived_at IS NULL"),
    )
    op.create_index(
        "uq_vat_rates_organization_id_name_lower",
        "vat_rates",
        ["organization_id", sa.literal_column("lower(name)")],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.create_table(
        "services",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("billing_basis", sa.Text(), server_default="hourly", nullable=False),
        sa.Column("default_fee", sa.BigInteger(), nullable=True),
        sa.Column("vat_rate_id", sa.UUID(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
            "billing_basis IN ('hourly', 'fixed')", name=op.f("ck_services_billing_basis")
        ),
        sa.CheckConstraint("code ~ '^[A-Z0-9_-]{1,12}$'", name=op.f("ck_services_code_format")),
        sa.CheckConstraint(
            "default_fee IS NULL OR default_fee >= 0", name=op.f("ck_services_default_fee")
        ),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 120", name=op.f("ck_services_name_length")),
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
    op.create_table(
        "clients",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("client_type", sa.Text(), server_default="limited_company", nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("address_line1", sa.Text(), nullable=True),
        sa.Column("address_line2", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("postcode", sa.Text(), nullable=True),
        sa.Column("country", sa.Text(), server_default="GB", nullable=False),
        sa.Column("company_number", sa.Text(), nullable=True),
        sa.Column("utr", sa.Text(), nullable=True),
        sa.Column("vat_number", sa.Text(), nullable=True),
        sa.Column("year_end_month", sa.SmallInteger(), nullable=True),
        sa.Column("year_end_day", sa.SmallInteger(), nullable=True),
        sa.Column("partner_user_id", sa.UUID(), nullable=True),
        sa.Column("manager_user_id", sa.UUID(), nullable=True),
        sa.Column("payment_terms_days", sa.SmallInteger(), nullable=True),
        sa.Column("default_vat_rate_id", sa.UUID(), nullable=True),
        sa.Column("billing_notes", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
            "client_type IN ('individual', 'sole_trader', 'partnership', 'llp', 'limited_company', 'trust', 'charity', 'other')",
            name=op.f("ck_clients_client_type"),
        ),
        sa.CheckConstraint("code ~ '^[A-Z0-9_-]{1,20}$'", name=op.f("ck_clients_code_format")),
        sa.CheckConstraint(
            "status IN ('prospect', 'active', 'inactive', 'archived')",
            name=op.f("ck_clients_status"),
        ),
        sa.CheckConstraint(
            "(year_end_month IS NULL) = (year_end_day IS NULL)",
            name=op.f("ck_clients_year_end_complete"),
        ),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 200", name=op.f("ck_clients_name_length")),
        sa.CheckConstraint(
            "payment_terms_days IS NULL OR payment_terms_days BETWEEN 0 AND 365",
            name=op.f("ck_clients_payment_terms_days"),
        ),
        sa.CheckConstraint("year_end_day BETWEEN 1 AND 31", name=op.f("ck_clients_year_end_day")),
        sa.CheckConstraint(
            "year_end_month BETWEEN 1 AND 12", name=op.f("ck_clients_year_end_month")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "default_vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_clients_organization_id_default_vat_rate_id_vat_rates",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "manager_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_clients_organization_id_manager_user_id_organization_members",
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
        "ix_clients_organization_id_manager_user_id",
        "clients",
        ["organization_id", "manager_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_clients_organization_id_name_lower",
        "clients",
        ["organization_id", sa.literal_column("lower(name)")],
        unique=False,
    )
    op.create_index(
        "uq_clients_organization_id_code",
        "clients",
        ["organization_id", "code"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "staff_rates",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("charge_rate", sa.BigInteger(), nullable=False),
        sa.Column("cost_rate", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
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
        sa.CheckConstraint("charge_rate >= 0", name=op.f("ck_staff_rates_charge_rate")),
        sa.CheckConstraint("cost_rate >= 0", name=op.f("ck_staff_rates_cost_rate")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_staff_rates_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_staff_rates_organization_id_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_staff_rates_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_rates")),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "effective_from",
            name="uq_staff_rates_organization_id_user_id_effective_from",
        ),
    )
    op.create_table(
        "client_contacts",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("position", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_billing", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
            "length(name) BETWEEN 1 AND 120", name=op.f("ck_client_contacts_name_length")
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
        ["client_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )
    op.create_table(
        "client_rates",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("hourly_rate", sa.BigInteger(), nullable=False),
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
        sa.CheckConstraint("hourly_rate >= 0", name=op.f("ck_client_rates_hourly_rate")),
        sa.ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_client_rates_organization_id_client_id_clients",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_client_rates_organization_id_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_client_rates_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_rates")),
    )
    op.create_index(
        "uq_client_rates_client_id_everyone",
        "client_rates",
        ["client_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.create_index(
        "uq_client_rates_client_id_user_id",
        "client_rates",
        ["client_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    add_updated_at_triggers(*TRIGGER_TABLES)


def downgrade() -> None:
    op.drop_index(
        "uq_client_rates_client_id_user_id",
        table_name="client_rates",
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.drop_index(
        "uq_client_rates_client_id_everyone",
        table_name="client_rates",
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.drop_table("client_rates")
    op.drop_index(
        "uq_client_contacts_client_id_primary",
        table_name="client_contacts",
        postgresql_where=sa.text("is_primary"),
    )
    op.drop_index("ix_client_contacts_organization_id_client_id", table_name="client_contacts")
    op.drop_table("client_contacts")
    op.drop_table("staff_rates")
    op.drop_index(
        "uq_clients_organization_id_code",
        table_name="clients",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index("ix_clients_organization_id_name_lower", table_name="clients")
    op.drop_index("ix_clients_organization_id_manager_user_id", table_name="clients")
    op.drop_table("clients")
    op.drop_index("uq_services_organization_id_code", table_name="services")
    op.drop_table("services")
    op.drop_index(
        "uq_vat_rates_organization_id_name_lower",
        table_name="vat_rates",
        postgresql_where=sa.text("archived_at IS NULL"),
    )
    op.drop_index(
        "uq_vat_rates_organization_id_default",
        table_name="vat_rates",
        postgresql_where=sa.text("is_default AND archived_at IS NULL"),
    )
    op.drop_table("vat_rates")
    op.drop_table("document_sequences")
    op.drop_index("uq_activity_codes_organization_id_code", table_name="activity_codes")
    op.drop_table("activity_codes")
