"""wip write-off approval threshold percent

Revision ID: 044
Revises: 043

Adds ``organizations.wip_write_off_approval_threshold_percent``: a write-off above this
percentage (basis points) of the job's or client's current WIP also needs approval, in
addition to the existing flat-pence ``wip_adjustment_approval_threshold``. Null (never set)
turns the percentage rule off; new practices default to 1000 (10%), matching the client's
documented assumption ("write-off approval needed above 10% or £500").
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "044"
down_revision: str | None = "043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "wip_write_off_approval_threshold_percent",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("1000"),
        ),
    )
    op.create_check_constraint(
        "wip_write_off_approval_threshold_percent",
        "organizations",
        "wip_write_off_approval_threshold_percent IS NULL OR "
        "wip_write_off_approval_threshold_percent BETWEEN 0 AND 10000",
    )


def downgrade() -> None:
    op.drop_constraint("wip_write_off_approval_threshold_percent", "organizations", type_="check")
    op.drop_column("organizations", "wip_write_off_approval_threshold_percent")
