"""document number suffix

Revision ID: 056
Revises: 055

Adds ``document_sequences.suffix``: text printed after the number of an invoice, credit note or
estimate (``INV-00042/26``). Empty for every existing sequence, so numbers do not change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "056"
down_revision: str | None = "055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "document_sequences",
        sa.Column("suffix", sa.Text(), nullable=False, server_default=""),
    )
    op.create_check_constraint(
        "suffix_format", "document_sequences", "suffix ~ '^[A-Za-z0-9/_-]{0,12}$'"
    )


def downgrade() -> None:
    op.drop_constraint("suffix_format", "document_sequences", type_="check")
    op.drop_column("document_sequences", "suffix")
