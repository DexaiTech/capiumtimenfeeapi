"""permissions for one person, on top of their role

``member_permission_overrides``: a permission granted to, or taken away from, one member. A person's
permissions are their role's, plus what is granted to them, less what is denied. Deleting the member
or a permission removes its overrides.

Revision ID: 055
Revises: 054
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "055"
down_revision: str | None = "054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "member_permission_overrides",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("permission_code", sa.Text(), nullable=False),
        sa.Column("effect", sa.Text(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "effect IN ('grant', 'deny')", name=op.f("ck_member_permission_overrides_effect")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_member_permission_overrides_organization_members",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["permission_code"],
            ["permissions.code"],
            name=op.f("fk_member_permission_overrides_permission_code_permissions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_member_permission_overrides")),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "permission_code",
            name="uq_member_permission_overrides_member_code",
        ),
    )


def downgrade() -> None:
    op.drop_table("member_permission_overrides")
