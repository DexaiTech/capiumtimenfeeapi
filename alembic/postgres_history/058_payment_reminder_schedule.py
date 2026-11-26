"""payment reminder schedule

Revision ID: 058
Revises: 057

Adds ``organizations.payment_reminder_schedule``: the days, counted from an invoice's due date,
on which a client is reminded until the invoice is paid (negative: before the due date, 0: on it,
positive: after it). Empty means the single reminder of ``payment_reminder_days_after_due``, so
every practice keeps behaving as before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "058"
down_revision: str | None = "057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "payment_reminder_schedule",
            postgresql.ARRAY(sa.SmallInteger()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.create_check_constraint(
        "payment_reminder_schedule",
        "organizations",
        "cardinality(payment_reminder_schedule) <= 12",
    )


def downgrade() -> None:
    op.drop_constraint("payment_reminder_schedule", "organizations", type_="check")
    op.drop_column("organizations", "payment_reminder_schedule")
