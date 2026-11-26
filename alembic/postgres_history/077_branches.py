"""branches

Revision ID: 077
Revises: 076

Ticket APT74: a practice can group its clients by branch (office). Adds ``branches`` (the
practice's own list, managed in the settings like the client segments), ``clients.branch_id``
(nullable: no client has a branch yet) and ``organizations.branches_enabled``, off for every
practice so nothing changes until one switches it on. On, every client created or edited needs a
branch.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "077"
down_revision: str | None = "076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "branches",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.CheckConstraint("length(name) BETWEEN 1 AND 40", name=op.f("ck_branches_name_length")),
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
        ["organization_id", sa.literal_column("lower(name)")],
        unique=True,
    )
    add_updated_at_triggers("branches")

    op.add_column("clients", sa.Column("branch_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_clients_organization_id_branch_id_branches",
        "clients",
        "branches",
        ["organization_id", "branch_id"],
        ["organization_id", "id"],
    )
    op.create_index(
        "ix_clients_organization_id_branch_id", "clients", ["organization_id", "branch_id"]
    )

    op.add_column(
        "organizations",
        sa.Column("branches_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("organizations", "branches_enabled")
    op.drop_index("ix_clients_organization_id_branch_id", table_name="clients")
    op.drop_constraint(
        "fk_clients_organization_id_branch_id_branches", "clients", type_="foreignkey"
    )
    op.drop_column("clients", "branch_id")
    op.drop_table("branches")
