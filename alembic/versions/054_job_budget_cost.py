"""the budgeted cost of a job

``jobs.budget_cost`` (pence, nullable) is the cost the practice expects the work to take at the
service level it sells it at. It is compared with the cost of the people who did the work (the
actual cost) and with the price agreed with the client on the billing summary.

Revision ID: 054
Revises: 053
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "054"
down_revision: str | None = "053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("budget_cost", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_jobs_budget_cost"), "jobs", "budget_cost IS NULL OR budget_cost >= 0"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_jobs_budget_cost"), "jobs", type_="check")
    op.drop_column("jobs", "budget_cost")
