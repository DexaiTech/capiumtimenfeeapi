"""invoice template colour and watermark

Revision ID: 059
Revises: 058

Adds ``invoice_templates.accent_color`` (``#RRGGBB``: the title, the practice name and the table
headings of a document) and ``watermark_text`` (printed large and pale across every page).
Both are optional: a template without them looks as it did.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "059"
down_revision: str | None = "058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invoice_templates", sa.Column("accent_color", sa.Text(), nullable=True))
    op.add_column("invoice_templates", sa.Column("watermark_text", sa.Text(), nullable=True))
    op.create_check_constraint(
        "accent_color_format",
        "invoice_templates",
        "accent_color IS NULL OR accent_color ~ '^#[0-9A-Fa-f]{6}$'",
    )
    op.create_check_constraint(
        "watermark_text_format",
        "invoice_templates",
        "watermark_text IS NULL OR watermark_text ~ '^[A-Za-z0-9 -]{1,24}$'",
    )


def downgrade() -> None:
    op.drop_constraint("watermark_text_format", "invoice_templates", type_="check")
    op.drop_constraint("accent_color_format", "invoice_templates", type_="check")
    op.drop_column("invoice_templates", "watermark_text")
    op.drop_column("invoice_templates", "accent_color")
