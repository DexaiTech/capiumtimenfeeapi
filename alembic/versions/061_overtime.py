"""overtime

Revision ID: 061
Revises: 060

A practice can switch overtime on (``organizations.overtime_enabled``, off unless it does). Time
marked ``time_entries.is_overtime`` is then priced at the normal rate times a multiplier, for
both the charge-out and the cost rate: ``organizations.overtime_multiplier_bp`` for everyone
(basis points, 15000 = 1.5x) unless a staff member has their own
(``organization_members.overtime_multiplier_bp``). With overtime off nothing is priced
differently.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "061"
down_revision: str | None = "060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("overtime_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "organizations",
        sa.Column("overtime_multiplier_bp", sa.Integer(), nullable=False, server_default="15000"),
    )
    op.add_column(
        "organization_members", sa.Column("overtime_multiplier_bp", sa.Integer(), nullable=True)
    )
    op.add_column(
        "time_entries",
        sa.Column("is_overtime", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_check_constraint(
        op.f("ck_organizations_overtime_multiplier_bp"),
        "organizations",
        "overtime_multiplier_bp BETWEEN 10000 AND 100000",
    )
    op.create_check_constraint(
        op.f("ck_organization_members_overtime_multiplier_bp"),
        "organization_members",
        "overtime_multiplier_bp IS NULL OR overtime_multiplier_bp BETWEEN 10000 AND 100000",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_organization_members_overtime_multiplier_bp"),
        "organization_members",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_organizations_overtime_multiplier_bp"), "organizations", type_="check"
    )
    op.drop_column("time_entries", "is_overtime")
    op.drop_column("organization_members", "overtime_multiplier_bp")
    op.drop_column("organizations", "overtime_multiplier_bp")
    op.drop_column("organizations", "overtime_enabled")
