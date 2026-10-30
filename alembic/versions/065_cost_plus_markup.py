"""the cost-plus markup of the recovery analysis

``organizations.cost_plus_markup_bp`` (basis points, 0 to 100000; 5000 = 50%) is the margin a
practice adds on top of the cost of its people when it bills "cost plus": time that cost 50.00 an
hour is worth 75.00 an hour at the default. The recovery analysis (the client's Invoices &
Recovery, the invoicing screen and the Recovery analysis report) compares what was invoiced with
the cost plus this markup, beside the task rate and the staff charge-out rate.

Revision ID: 065
Revises: 064
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "065"
down_revision: str | None = "064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("cost_plus_markup_bp", sa.Integer(), nullable=False, server_default="5000"),
    )
    op.create_check_constraint(
        op.f("ck_organizations_cost_plus_markup_bp"),
        "organizations",
        "cost_plus_markup_bp BETWEEN 0 AND 100000",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_organizations_cost_plus_markup_bp"), "organizations", type_="check")
    op.drop_column("organizations", "cost_plus_markup_bp")
