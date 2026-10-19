"""integrations: the per-practice connection settings and the sync-item outbox

Adds ``integration_connections`` (a practice's switch, settings and last outcome for one
integration: central client, central user, practice management, bookkeeping) and
``integration_sync_items`` (one thing to send to, or apply from, another system: unique per
practice, integration and idempotency key, with attempts and a retry schedule).

Revision ID: 039
Revises: 038
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.migration_helpers import add_updated_at_triggers, timestamps

revision: str = "039"
down_revision: str | None = "038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEYS = "'central_client', 'central_user', 'practice_management', 'bookkeeping'"


def _organization_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.create_table(
        "integration_connections",
        sa.Column("integration", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.Text(), server_default="idle", nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "config",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *timestamps(),
        sa.CheckConstraint(
            f"integration IN ({KEYS})", name=op.f("ck_integration_connections_integration")
        ),
        sa.CheckConstraint(
            "status IN ('idle', 'ok', 'error')", name=op.f("ck_integration_connections_status")
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR length(last_error) <= 1000",
            name=op.f("ck_integration_connections_last_error_length"),
        ),
        _organization_fk("integration_connections"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_connections")),
        sa.UniqueConstraint(
            "organization_id",
            "integration",
            name="uq_integration_connections_organization_id_integration",
        ),
    )

    op.create_table(
        "integration_sync_items",
        sa.Column("integration", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column("direction", sa.Text(), server_default="outbound", nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("external_ref", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *timestamps(),
        sa.CheckConstraint(
            f"integration IN ({KEYS})", name=op.f("ck_integration_sync_items_integration")
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')",
            name=op.f("ck_integration_sync_items_direction"),
        ),
        sa.CheckConstraint(
            "length(operation) BETWEEN 1 AND 60",
            name=op.f("ck_integration_sync_items_operation_length"),
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200",
            name=op.f("ck_integration_sync_items_idempotency_key_length"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ok', 'failed', 'skipped')",
            name=op.f("ck_integration_sync_items_status"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_integration_sync_items_attempts")),
        sa.CheckConstraint(
            "(status = 'pending') = (next_attempt_at IS NOT NULL)",
            name=op.f("ck_integration_sync_items_next_attempt"),
        ),
        sa.CheckConstraint(
            "last_error IS NULL OR length(last_error) <= 1000",
            name=op.f("ck_integration_sync_items_last_error_length"),
        ),
        _organization_fk("integration_sync_items"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_sync_items")),
        sa.UniqueConstraint(
            "organization_id",
            "integration",
            "idempotency_key",
            name="uq_integration_sync_items_organization_id_idempotency_key",
        ),
    )
    op.create_index(
        "ix_integration_sync_items_due",
        "integration_sync_items",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
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
        postgresql_where=sa.text("entity_id IS NOT NULL"),
    )

    add_updated_at_triggers("integration_connections", "integration_sync_items")


def downgrade() -> None:
    op.drop_table("integration_sync_items")
    op.drop_table("integration_connections")
