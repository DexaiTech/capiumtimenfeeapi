"""central clients: the cache, the synchronisation log, the change-feed cursor and the client link

Adds ``central_client_cache`` (the last known state of each central client a practice has seen),
``integration_sync_logs`` (one row per synchronisation attempt; generic, the other integrations
reuse it), ``integration_cursors`` (how far a practice has read a change feed) and, on
``clients``, ``central_client_id`` (partial unique per practice among live clients),
``central_synced_at``, ``central_sync_state`` and ``merged_into_client_id``.

Revision ID: 016
Revises: 015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "016"
down_revision: str | None = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _organization_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.create_table(
        "central_client_cache",
        sa.Column("central_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("company_number", sa.Text(), nullable=True),
        sa.Column("address_line1", sa.Text(), nullable=True),
        sa.Column("address_line2", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("postcode", sa.Text(), nullable=True),
        sa.Column("country", sa.Text(), server_default="GB", nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "length(central_id) BETWEEN 1 AND 64",
            name=op.f("ck_central_client_cache_central_id_length"),
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 200", name=op.f("ck_central_client_cache_name_length")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'archived')", name=op.f("ck_central_client_cache_status")
        ),
        _organization_fk("central_client_cache"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_central_client_cache")),
        sa.UniqueConstraint(
            "organization_id",
            "central_id",
            name="uq_central_client_cache_organization_id_central_id",
        ),
    )
    op.create_index(
        "ix_central_client_cache_organization_id_name_lower",
        "central_client_cache",
        ["organization_id", sa.literal_column("lower(name)")],
        unique=False,
    )
    op.create_index(
        "ix_central_client_cache_organization_id_company_number",
        "central_client_cache",
        ["organization_id", "company_number"],
        unique=False,
    )

    op.create_table(
        "integration_sync_logs",
        sa.Column("integration", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), server_default="inbound", nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("external_event_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "integration ~ '^[a-z][a-z0-9_]{1,39}$'",
            name=op.f("ck_integration_sync_logs_integration_format"),
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')",
            name=op.f("ck_integration_sync_logs_direction"),
        ),
        sa.CheckConstraint(
            "length(operation) BETWEEN 1 AND 60",
            name=op.f("ck_integration_sync_logs_operation_length"),
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'failed', 'skipped')", name=op.f("ck_integration_sync_logs_status")
        ),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_integration_sync_logs_attempt")),
        sa.CheckConstraint(
            "message IS NULL OR length(message) <= 1000",
            name=op.f("ck_integration_sync_logs_message_length"),
        ),
        _organization_fk("integration_sync_logs"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_sync_logs")),
    )
    op.create_index(
        "uq_integration_sync_logs_event",
        "integration_sync_logs",
        ["organization_id", "integration", "external_event_id"],
        unique=True,
        postgresql_where=sa.text("external_event_id IS NOT NULL"),
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
        postgresql_where=sa.text("entity_id IS NOT NULL"),
    )

    op.create_table(
        "integration_cursors",
        sa.Column("integration", sa.Text(), nullable=False),
        sa.Column("cursor", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "integration ~ '^[a-z][a-z0-9_]{1,39}$'",
            name=op.f("ck_integration_cursors_integration_format"),
        ),
        _organization_fk("integration_cursors"),
        sa.PrimaryKeyConstraint(
            "organization_id", "integration", name=op.f("pk_integration_cursors")
        ),
    )

    op.add_column("clients", sa.Column("central_client_id", sa.Text(), nullable=True))
    op.add_column(
        "clients", sa.Column("central_synced_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "clients",
        sa.Column("central_sync_state", sa.Text(), server_default="local", nullable=False),
    )
    op.add_column("clients", sa.Column("merged_into_client_id", sa.UUID(), nullable=True))
    op.create_check_constraint(
        op.f("ck_clients_central_sync_state"),
        "clients",
        "central_sync_state IN ('local', 'synced', 'stale', 'failed')",
    )
    op.create_check_constraint(
        op.f("ck_clients_central_client_id_length"),
        "clients",
        "central_client_id IS NULL OR length(central_client_id) BETWEEN 1 AND 64",
    )
    op.create_check_constraint(
        op.f("ck_clients_central_state_matches_link"),
        "clients",
        "(central_client_id IS NULL) = (central_sync_state = 'local')",
    )
    op.create_check_constraint(
        op.f("ck_clients_not_merged_into_itself"),
        "clients",
        "merged_into_client_id IS NULL OR merged_into_client_id <> id",
    )
    op.create_foreign_key(
        "fk_clients_organization_id_merged_into_client_id_clients",
        "clients",
        "clients",
        ["organization_id", "merged_into_client_id"],
        ["organization_id", "id"],
    )
    op.create_index(
        "uq_clients_organization_id_central_client_id",
        "clients",
        ["organization_id", "central_client_id"],
        unique=True,
        postgresql_where=sa.text("central_client_id IS NOT NULL AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_clients_organization_id_merged_into_client_id",
        "clients",
        ["organization_id", "merged_into_client_id"],
        unique=False,
        postgresql_where=sa.text("merged_into_client_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_clients_organization_id_merged_into_client_id", table_name="clients")
    op.drop_index("uq_clients_organization_id_central_client_id", table_name="clients")
    op.drop_constraint(
        "fk_clients_organization_id_merged_into_client_id_clients", "clients", type_="foreignkey"
    )
    for name in (
        "ck_clients_not_merged_into_itself",
        "ck_clients_central_state_matches_link",
        "ck_clients_central_client_id_length",
        "ck_clients_central_sync_state",
    ):
        op.drop_constraint(op.f(name), "clients", type_="check")
    op.drop_column("clients", "merged_into_client_id")
    op.drop_column("clients", "central_sync_state")
    op.drop_column("clients", "central_synced_at")
    op.drop_column("clients", "central_client_id")

    op.drop_table("integration_cursors")
    op.drop_table("integration_sync_logs")
    op.drop_table("central_client_cache")
