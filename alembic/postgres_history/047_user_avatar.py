"""profile pictures

Revision ID: 047
Revises: 046

Adds ``users.avatar_storage_key``, ``avatar_content_type`` and ``avatar_updated_at``: the
profile picture of a person, kept in the file store (S3 or ``STORAGE_DIR``) under a random key.
``avatar_updated_at`` doubles as "has a picture" and as the cache-buster of the image.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "047"
down_revision: str | None = "046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_storage_key", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("avatar_content_type", sa.Text(), nullable=True))
    op.add_column(
        "users", sa.Column("avatar_updated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "avatar_all_or_none",
        "users",
        "(avatar_storage_key IS NULL) = (avatar_content_type IS NULL) "
        "AND (avatar_storage_key IS NULL) = (avatar_updated_at IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("avatar_all_or_none", "users", type_="check")
    op.drop_column("users", "avatar_updated_at")
    op.drop_column("users", "avatar_content_type")
    op.drop_column("users", "avatar_storage_key")
