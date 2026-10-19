"""services and activity types: billing defaults, unique names and service-to-activity links

Services get a default billable status and an invoice description; activity types get a default
VAT rate and an invoice description. Names become unique per practice (case-insensitively;
existing duplicates are renamed ``Name (2)``, ``Name (3)``...). ``service_activity_codes`` holds
the activity types a service allows (no rows = every type).

Revision ID: 012
Revises: 011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: str | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _dedupe_names(table: str) -> None:
    op.execute(
        f"""
        UPDATE {table} t
        SET name = left(t.name, 110) || ' (' || d.rn || ')'
        FROM (
            SELECT id, row_number() OVER (
                PARTITION BY organization_id, lower(name) ORDER BY created_at, id
            ) AS rn
            FROM {table}
        ) d
        WHERE d.id = t.id AND d.rn > 1
        """
    )


def upgrade() -> None:
    op.add_column(
        "services",
        sa.Column("default_billable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column("services", sa.Column("invoice_description", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_services_invoice_description_length"),
        "services",
        "invoice_description IS NULL OR length(invoice_description) BETWEEN 1 AND 500",
    )
    _dedupe_names("services")
    op.create_index(
        "uq_services_organization_id_name_lower",
        "services",
        ["organization_id", sa.literal_column("lower(name)")],
        unique=True,
    )

    op.add_column("activity_codes", sa.Column("vat_rate_id", sa.UUID(), nullable=True))
    op.add_column("activity_codes", sa.Column("invoice_description", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_activity_codes_invoice_description_length"),
        "activity_codes",
        "invoice_description IS NULL OR length(invoice_description) BETWEEN 1 AND 500",
    )
    op.create_foreign_key(
        "fk_activity_codes_organization_id_vat_rate_id_vat_rates",
        "activity_codes",
        "vat_rates",
        ["organization_id", "vat_rate_id"],
        ["organization_id", "id"],
    )
    _dedupe_names("activity_codes")
    op.create_index(
        "uq_activity_codes_organization_id_name_lower",
        "activity_codes",
        ["organization_id", sa.literal_column("lower(name)")],
        unique=True,
    )

    op.create_table(
        "service_activity_codes",
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("activity_code_id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
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


def downgrade() -> None:
    op.drop_index(
        "ix_service_activity_codes_organization_id_activity_code_id",
        table_name="service_activity_codes",
    )
    op.drop_table("service_activity_codes")
    op.drop_index("uq_activity_codes_organization_id_name_lower", table_name="activity_codes")
    op.drop_constraint(
        "fk_activity_codes_organization_id_vat_rate_id_vat_rates",
        "activity_codes",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_activity_codes_invoice_description_length"), "activity_codes", type_="check"
    )
    op.drop_column("activity_codes", "invoice_description")
    op.drop_column("activity_codes", "vat_rate_id")
    op.drop_index("uq_services_organization_id_name_lower", table_name="services")
    op.drop_constraint(op.f("ck_services_invoice_description_length"), "services", type_="check")
    op.drop_column("services", "invoice_description")
    op.drop_column("services", "default_billable")
