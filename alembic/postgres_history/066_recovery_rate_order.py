"""recovery rate priority and client and job cost-plus mark-ups

Revision ID: 066
Revises: 065

The client's feedback on the recovery analysis (ticket APT52):

* ``organizations.recovery_rate_order``: the order in which the recovery analysis looks for the
  rate a piece of work could have been billed at: ``client_job`` (the rate agreed with the client
  for the job: a client rate, else the job's own rate), ``task`` (the rate set against the job's
  service) and ``staff`` (the person's charge-out rate). Default ``{client_job,task,staff}``; any
  order of the three, each once.
* ``clients.cost_plus_markup_bp`` and ``jobs.cost_plus_markup_bp`` (basis points, 0 to 100000,
  null = not set): the cost-plus mark-up of a client and of a job. The job's wins over the
  client's, which wins over the practice's ``organizations.cost_plus_markup_bp`` (migration 065).

Nothing else changes: no recorded figure is rewritten.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "066"
down_revision: str | None = "065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORDER_CHECK = (
    "cardinality(recovery_rate_order) = 3 "
    "AND recovery_rate_order @> ARRAY['client_job', 'task', 'staff']::text[]"
)


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "recovery_rate_order",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{client_job,task,staff}'::text[]"),
        ),
    )
    op.create_check_constraint(
        op.f("ck_organizations_recovery_rate_order"), "organizations", _ORDER_CHECK
    )
    for table in ("clients", "jobs"):
        op.add_column(table, sa.Column("cost_plus_markup_bp", sa.Integer(), nullable=True))
        op.create_check_constraint(
            op.f(f"ck_{table}_cost_plus_markup_bp"),
            table,
            "cost_plus_markup_bp IS NULL OR cost_plus_markup_bp BETWEEN 0 AND 100000",
        )


def downgrade() -> None:
    for table in ("jobs", "clients"):
        op.drop_constraint(op.f(f"ck_{table}_cost_plus_markup_bp"), table, type_="check")
        op.drop_column(table, "cost_plus_markup_bp")
    op.drop_constraint(op.f("ck_organizations_recovery_rate_order"), "organizations", type_="check")
    op.drop_column("organizations", "recovery_rate_order")
