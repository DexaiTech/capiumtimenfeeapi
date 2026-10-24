"""jobs from client engagements

Revision ID: 051
Revises: 050

Adds ``auto_create_jobs`` and ``job_lead_days`` to ``client_services`` (an engagement opts in to
having the job of each period created by the daily worker) and ``engagement_job_runs``: one row
per (engagement, period start), so a repeated run never creates a period twice, a failed period is
visible, and a deleted job is not recreated (``job_id`` is set null). Additive: existing
engagements keep ``auto_create_jobs = false``. The way down drops the table and the columns.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "051"
down_revision: str | None = "050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "engagement_job_runs"


def upgrade() -> None:
    op.add_column(
        "client_services",
        sa.Column(
            "auto_create_jobs", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "client_services",
        sa.Column("job_lead_days", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_client_services_job_lead_days"),
        "client_services",
        "job_lead_days BETWEEN 0 AND 365",
    )
    op.create_table(
        TABLE,
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("client_service_id", sa.UUID(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('created', 'failed')", name=op.f(f"ck_{TABLE}_status")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f(f"fk_{TABLE}_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["client_service_id"],
            ["client_services.id"],
            name=op.f(f"fk_{TABLE}_client_service_id_client_services"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["jobs.id"],
            name=op.f(f"fk_{TABLE}_job_id_jobs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{TABLE}")),
        sa.UniqueConstraint("client_service_id", "period_start", name=op.f(f"uq_{TABLE}_period")),
    )
    op.create_index(
        op.f(f"ix_{TABLE}_organization_id_client_service_id"),
        TABLE,
        ["organization_id", "client_service_id"],
    )
    add_updated_at_triggers(TABLE)


def downgrade() -> None:
    op.drop_table(TABLE)
    op.drop_constraint(op.f("ck_client_services_job_lead_days"), "client_services", type_="check")
    op.drop_column("client_services", "job_lead_days")
    op.drop_column("client_services", "auto_create_jobs")
