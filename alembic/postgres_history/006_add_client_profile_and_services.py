"""add client profile and services

Revision ID: 006
Revises: 005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_trigger

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_services",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("fee", sa.BigInteger(), nullable=True),
        sa.Column("frequency", sa.Text(), server_default="annually", nullable=False),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("end_date", sa.Date(), nullable=True),
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
            "frequency IN ('one_off', 'monthly', 'quarterly', 'annually')",
            name=op.f("ck_client_services_frequency"),
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR start_date IS NULL OR start_date <= end_date",
            name=op.f("ck_client_services_date_order"),
        ),
        sa.CheckConstraint("fee IS NULL OR fee >= 0", name=op.f("ck_client_services_fee")),
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
    op.add_column("clients", sa.Column("segment", sa.Text(), nullable=True))
    op.add_column(
        "clients", sa.Column("aml_status", sa.Text(), server_default="not_started", nullable=False)
    )
    op.add_column("clients", sa.Column("aml_checked_on", sa.Date(), nullable=True))
    op.add_column("clients", sa.Column("health_score", sa.SmallInteger(), nullable=True))
    op.add_column("clients", sa.Column("accounting_software", sa.Text(), nullable=True))
    add_updated_at_trigger("client_services")


def downgrade() -> None:
    op.drop_column("clients", "accounting_software")
    op.drop_column("clients", "health_score")
    op.drop_column("clients", "aml_checked_on")
    op.drop_column("clients", "aml_status")
    op.drop_column("clients", "segment")
    op.drop_index("ix_client_services_organization_id_service_id", table_name="client_services")
    op.drop_table("client_services")
