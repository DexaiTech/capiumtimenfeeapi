"""invoice templates and the client billing settings

Adds ``invoice_templates`` (a ``Standard`` default template is created for every existing
practice; new practices get theirs when their defaults are seeded) and, on ``clients``,
``invoice_email``, ``default_service_id`` and ``invoice_template_id``.

Revision ID: 017
Revises: 016
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "017"
down_revision: str | None = "016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invoice_templates",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("layout", sa.Text(), server_default="standard", nullable=False),
        sa.Column("show_logo", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "show_bank_details", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("footer_text", sa.Text(), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
            "length(name) BETWEEN 1 AND 80", name=op.f("ck_invoice_templates_name_length")
        ),
        sa.CheckConstraint(
            "layout IN ('standard', 'detailed', 'summary')",
            name=op.f("ck_invoice_templates_layout"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'inactive')", name=op.f("ck_invoice_templates_status")
        ),
        sa.CheckConstraint(
            "footer_text IS NULL OR length(footer_text) BETWEEN 1 AND 1000",
            name=op.f("ck_invoice_templates_footer_text_length"),
        ),
        sa.CheckConstraint(
            "NOT is_default OR status = 'active'",
            name=op.f("ck_invoice_templates_default_is_active"),
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
        "uq_invoice_templates_organization_id_name_lower",
        "invoice_templates",
        ["organization_id", sa.literal_column("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "uq_invoice_templates_organization_id_default",
        "invoice_templates",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("is_default AND deleted_at IS NULL"),
    )
    add_updated_at_triggers("invoice_templates")

    op.execute(
        """
        INSERT INTO invoice_templates (id, organization_id, name, layout, show_logo,
                                       show_bank_details, is_default, status)
        SELECT gen_random_uuid(), id, 'Standard', 'standard', true, true, true, 'active'
        FROM organizations
        """
    )

    op.add_column("clients", sa.Column("invoice_email", sa.Text(), nullable=True))
    op.add_column("clients", sa.Column("default_service_id", sa.UUID(), nullable=True))
    op.add_column("clients", sa.Column("invoice_template_id", sa.UUID(), nullable=True))
    op.create_check_constraint(
        op.f("ck_clients_invoice_email_length"),
        "clients",
        "invoice_email IS NULL OR length(invoice_email) BETWEEN 3 AND 254",
    )
    op.create_foreign_key(
        "fk_clients_organization_id_default_service_id_services",
        "clients",
        "services",
        ["organization_id", "default_service_id"],
        ["organization_id", "id"],
    )
    op.create_foreign_key(
        "fk_clients_invoice_template_invoice_templates",
        "clients",
        "invoice_templates",
        ["organization_id", "invoice_template_id"],
        ["organization_id", "id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_clients_invoice_template_invoice_templates",
        "clients",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_clients_organization_id_default_service_id_services", "clients", type_="foreignkey"
    )
    op.drop_constraint(op.f("ck_clients_invoice_email_length"), "clients", type_="check")
    op.drop_column("clients", "invoice_template_id")
    op.drop_column("clients", "default_service_id")
    op.drop_column("clients", "invoice_email")

    op.drop_index("uq_invoice_templates_organization_id_default", table_name="invoice_templates")
    op.drop_index("uq_invoice_templates_organization_id_name_lower", table_name="invoice_templates")
    op.drop_table("invoice_templates")
