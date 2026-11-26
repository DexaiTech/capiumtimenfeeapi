"""client activity: indexes that find the audit entries about one client

The client overview lists the latest audit entries about a client: those whose target is the
client and those that name it in their metadata (``client_id``: invoices, jobs, time, payments,
...). Both are partial indexes, so only the rows that can match are indexed.

Revision ID: 018
Revises: 017
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "018"
down_revision: str | None = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_audit_logs_organization_id_client_target_created_at",
        "audit_logs",
        ["organization_id", "target_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("target_type = 'client'"),
    )
    op.create_index(
        "ix_audit_logs_organization_id_client_ref_created_at",
        "audit_logs",
        ["organization_id", sa.literal_column("(metadata ->> 'client_id')"), "created_at"],
        unique=False,
        postgresql_where=sa.text("(metadata ->> 'client_id') IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_organization_id_client_ref_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_organization_id_client_target_created_at", table_name="audit_logs")
