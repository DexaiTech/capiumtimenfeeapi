"""expense receipts: the files that prove an expense line

Adds ``expense_receipts``: the name, the detected content type, the size (at most 10 MiB), the
SHA-256 and the random storage key of each PDF, JPEG or PNG; the bytes live in the file storage.

Revision ID: 026
Revises: 025
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "026"
down_revision: str | None = "025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MAX_RECEIPT_BYTES = 10 * 1024 * 1024


def upgrade() -> None:
    op.create_table(
        "expense_receipts",
        sa.Column("expense_id", sa.UUID(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("uploaded_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "length(file_name) BETWEEN 1 AND 200",
            name=op.f("ck_expense_receipts_file_name_length"),
        ),
        sa.CheckConstraint(
            "length(content_type) BETWEEN 3 AND 100",
            name=op.f("ck_expense_receipts_content_type_length"),
        ),
        sa.CheckConstraint(
            f"size_bytes BETWEEN 1 AND {MAX_RECEIPT_BYTES}",
            name=op.f("ck_expense_receipts_size_bytes"),
        ),
        sa.CheckConstraint(
            "length(storage_key) BETWEEN 16 AND 128",
            name=op.f("ck_expense_receipts_storage_key_length"),
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_expense_receipts_sha256_format")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "expense_id"],
            ["expenses.organization_id", "expenses.id"],
            name="fk_expense_receipts_expense_id_expenses",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "uploaded_by"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_expense_receipts_organization_id_uploader_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_expense_receipts_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expense_receipts")),
        sa.UniqueConstraint("organization_id", "id", name="uq_expense_receipts_organization_id_id"),
    )
    op.create_index(
        "uq_expense_receipts_storage_key", "expense_receipts", ["storage_key"], unique=True
    )
    op.create_index(
        "ix_expense_receipts_organization_id_expense_id",
        "expense_receipts",
        ["organization_id", "expense_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("expense_receipts")
