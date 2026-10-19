"""data import rows

Adds ``data_import_rows``: the data rows of an uploaded file with what the file said, what the
check made of each (state, action, messages) and the request body the existing service will
receive when the batch is committed (``docs/modules/import-export.md``). A batch's rows go
with the batch.

Revision ID: 038
Revises: 037
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "038"
down_revision: str | None = "037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_import_rows",
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("target_id", sa.UUID(), nullable=True),
        sa.Column(
            "messages",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("row_number >= 1", name=op.f("ck_data_import_rows_row_number")),
        sa.CheckConstraint(
            "state IN ('valid', 'warning', 'rejected')", name=op.f("ck_data_import_rows_state")
        ),
        sa.CheckConstraint(
            "action IS NULL OR action IN ('create', 'update', 'unchanged')",
            name=op.f("ck_data_import_rows_action"),
        ),
        sa.CheckConstraint(
            "state = 'rejected' OR action IS NOT NULL",
            name=op.f("ck_data_import_rows_importable_has_action"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_data_import_rows_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "batch_id"],
            ["data_import_batches.organization_id", "data_import_batches.id"],
            name="fk_data_import_rows_organization_id_data_import_batches",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_import_rows")),
        sa.UniqueConstraint(
            "batch_id", "row_number", name="uq_data_import_rows_batch_id_row_number"
        ),
    )


def downgrade() -> None:
    op.drop_table("data_import_rows")
