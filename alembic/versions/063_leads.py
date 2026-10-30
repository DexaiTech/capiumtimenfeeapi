"""leads

Revision ID: 063
Revises: 062

Adds ``leads``: prospective clients with what winning them cost (``acquisition_cost`` and
``onboarding_cost``, in pence). Converting a lead creates a client and records it in
``converted_client_id`` (with ``converted_at``); ``archived_at`` hides a lead from the open list.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_trigger

revision: str = "063"
down_revision: str | None = "062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("client_type", sa.Text(), server_default="limited_company", nullable=False),
        sa.Column("contact_name", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("acquisition_cost", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("onboarding_cost", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("converted_client_id", sa.UUID(), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
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
        sa.CheckConstraint("length(name) BETWEEN 1 AND 200", name=op.f("ck_leads_name_length")),
        sa.CheckConstraint(
            "client_type IN ('individual', 'sole_trader', 'partnership', 'llp', "
            "'limited_company', 'trust', 'charity', 'other')",
            name=op.f("ck_leads_client_type"),
        ),
        sa.CheckConstraint(
            "source IS NULL OR length(source) BETWEEN 1 AND 100",
            name=op.f("ck_leads_source_length"),
        ),
        sa.CheckConstraint("acquisition_cost >= 0", name=op.f("ck_leads_acquisition_cost")),
        sa.CheckConstraint("onboarding_cost >= 0", name=op.f("ck_leads_onboarding_cost")),
        sa.CheckConstraint(
            "(converted_client_id IS NULL) = (converted_at IS NULL)",
            name=op.f("ck_leads_converted_together"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_leads_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "created_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_leads_organization_id_created_by_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "converted_client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_leads_organization_id_converted_client_id_clients",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leads")),
        sa.UniqueConstraint("organization_id", "id", name="uq_leads_organization_id_id"),
    )
    op.create_index(
        "ix_leads_organization_id_created_at", "leads", ["organization_id", "created_at", "id"]
    )
    op.create_index(
        "uq_leads_organization_id_converted_client_id",
        "leads",
        ["organization_id", "converted_client_id"],
        unique=True,
        postgresql_where=sa.text("converted_client_id IS NOT NULL"),
    )
    add_updated_at_trigger("leads")


def downgrade() -> None:
    op.drop_table("leads")
