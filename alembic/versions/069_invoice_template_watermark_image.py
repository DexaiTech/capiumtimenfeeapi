"""invoice template watermark image

Revision ID: 069
Revises: 068

Ticket APT56: a template's watermark is a text or a picture. Adds
``invoice_templates.watermark_image_key`` (the picture in the file store, like a profile picture)
and ``watermark_image_type`` (its content type, found on upload), set together, and a check that a
template does not have both a watermark text and a picture.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "069"
down_revision: str | None = "068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invoice_templates", sa.Column("watermark_image_key", sa.Text(), nullable=True))
    op.add_column("invoice_templates", sa.Column("watermark_image_type", sa.Text(), nullable=True))
    op.create_check_constraint(
        "watermark_image_all_or_none",
        "invoice_templates",
        "(watermark_image_key IS NULL) = (watermark_image_type IS NULL)",
    )
    op.create_check_constraint(
        "watermark_text_or_image",
        "invoice_templates",
        "watermark_text IS NULL OR watermark_image_key IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint("watermark_text_or_image", "invoice_templates", type_="check")
    op.drop_constraint("watermark_image_all_or_none", "invoice_templates", type_="check")
    op.drop_column("invoice_templates", "watermark_image_type")
    op.drop_column("invoice_templates", "watermark_image_key")
