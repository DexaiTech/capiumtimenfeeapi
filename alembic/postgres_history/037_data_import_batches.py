"""data import batches

Adds ``data_import_batches``: one row per uploaded staff, rates or jobs file, with the outcome
of checking it, the options and the client/staff mapping the person chose, and what committing
it did (``docs/modules/import-export.md``). The rows of a file are in ``data_import_rows``
(migration 038).

Revision ID: 037
Revises: 036
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "037"
down_revision: str | None = "036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ZERO = sa.text("0")
_EMPTY_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "data_import_batches",
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("file_format", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("options", postgresql.JSONB(), server_default=_EMPTY_OBJECT, nullable=False),
        sa.Column("mapping", postgresql.JSONB(), server_default=_EMPTY_OBJECT, nullable=False),
        sa.Column("columns", postgresql.JSONB(), server_default=_EMPTY_OBJECT, nullable=False),
        sa.Column("total_rows", sa.Integer(), server_default=_ZERO, nullable=False),
        sa.Column("valid_rows", sa.Integer(), server_default=_ZERO, nullable=False),
        sa.Column("warning_rows", sa.Integer(), server_default=_ZERO, nullable=False),
        sa.Column("rejected_rows", sa.Integer(), server_default=_ZERO, nullable=False),
        sa.Column("created_rows", sa.Integer(), server_default=_ZERO, nullable=False),
        sa.Column("updated_rows", sa.Integer(), server_default=_ZERO, nullable=False),
        sa.Column("unchanged_rows", sa.Integer(), server_default=_ZERO, nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("committed_by", sa.UUID(), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
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
            "entity IN ('staff', 'rates', 'jobs')", name=op.f("ck_data_import_batches_entity")
        ),
        sa.CheckConstraint(
            "file_format IN ('csv', 'xlsx')", name=op.f("ck_data_import_batches_file_format")
        ),
        sa.CheckConstraint(
            "status IN ('validated', 'committed', 'failed')",
            name=op.f("ck_data_import_batches_status"),
        ),
        sa.CheckConstraint(
            "length(filename) BETWEEN 1 AND 255",
            name=op.f("ck_data_import_batches_filename_length"),
        ),
        sa.CheckConstraint(
            "total_rows >= 0 AND valid_rows >= 0 AND warning_rows >= 0 AND rejected_rows >= 0 "
            "AND created_rows >= 0 AND updated_rows >= 0 AND unchanged_rows >= 0",
            name=op.f("ck_data_import_batches_counts"),
        ),
        sa.CheckConstraint(
            "status <> 'committed' OR committed_at IS NOT NULL",
            name=op.f("ck_data_import_batches_committed_at"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_data_import_batches_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_data_import_batches_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["committed_by"],
            ["users.id"],
            name=op.f("fk_data_import_batches_committed_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_import_batches")),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_data_import_batches_organization_id_id"
        ),
    )
    op.create_index(
        "ix_data_import_batches_organization_id_created_at",
        "data_import_batches",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    add_updated_at_triggers("data_import_batches")


def downgrade() -> None:
    op.drop_table("data_import_batches")
