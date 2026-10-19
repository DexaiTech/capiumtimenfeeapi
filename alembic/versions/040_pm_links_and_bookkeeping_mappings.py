"""integrations: bookkeeping mappings and the practice management link of jobs and tasks

Adds ``bookkeeping_service_mappings`` (service -> income account; a row without a service is the
account for lines that have none) and ``bookkeeping_vat_mappings`` (VAT code -> tax code; a row
without a VAT code is the tax code for lines that carry none), and ``jobs.pm_job_id`` /
``pm_linked_at`` (unique among a practice's live jobs) and ``tasks.pm_task_id`` /
``pm_linked_at`` for the practice management link.

Revision ID: 040
Revises: 039
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers, timestamps

revision: str = "040"
down_revision: str | None = "039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CODE_PATTERN = "^[A-Za-z0-9][A-Za-z0-9._/-]{0,19}$"


def _organization_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.create_table(
        "bookkeeping_service_mappings",
        sa.Column("service_id", sa.UUID(), nullable=True),
        sa.Column("account_code", sa.Text(), nullable=False),
        sa.Column("account_name", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *timestamps(),
        sa.CheckConstraint(
            f"account_code ~ '{CODE_PATTERN}'",
            name=op.f("ck_bookkeeping_service_mappings_account_code_format"),
        ),
        sa.CheckConstraint(
            "account_name IS NULL OR length(account_name) BETWEEN 1 AND 120",
            name=op.f("ck_bookkeeping_service_mappings_account_name_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_bk_service_mappings_service",
            ondelete="CASCADE",
        ),
        _organization_fk("bookkeeping_service_mappings"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookkeeping_service_mappings")),
    )
    op.create_index(
        "uq_bk_service_mappings_service",
        "bookkeeping_service_mappings",
        ["organization_id", "service_id"],
        unique=True,
        postgresql_where=sa.text("service_id IS NOT NULL"),
    )
    op.create_index(
        "uq_bk_service_mappings_default",
        "bookkeeping_service_mappings",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("service_id IS NULL"),
    )

    op.create_table(
        "bookkeeping_vat_mappings",
        sa.Column("vat_rate_id", sa.UUID(), nullable=True),
        sa.Column("tax_code", sa.Text(), nullable=False),
        sa.Column("tax_name", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *timestamps(),
        sa.CheckConstraint(
            f"tax_code ~ '{CODE_PATTERN}'",
            name=op.f("ck_bookkeeping_vat_mappings_tax_code_format"),
        ),
        sa.CheckConstraint(
            "tax_name IS NULL OR length(tax_name) BETWEEN 1 AND 120",
            name=op.f("ck_bookkeeping_vat_mappings_tax_name_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_bk_vat_mappings_vat_rate",
            ondelete="CASCADE",
        ),
        _organization_fk("bookkeeping_vat_mappings"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookkeeping_vat_mappings")),
    )
    op.create_index(
        "uq_bk_vat_mappings_vat_rate",
        "bookkeeping_vat_mappings",
        ["organization_id", "vat_rate_id"],
        unique=True,
        postgresql_where=sa.text("vat_rate_id IS NOT NULL"),
    )
    op.create_index(
        "uq_bk_vat_mappings_default",
        "bookkeeping_vat_mappings",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("vat_rate_id IS NULL"),
    )
    add_updated_at_triggers("bookkeeping_service_mappings", "bookkeeping_vat_mappings")

    op.add_column("jobs", sa.Column("pm_job_id", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("pm_linked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_jobs_pm_link"), "jobs", "(pm_job_id IS NULL) = (pm_linked_at IS NULL)"
    )
    op.create_check_constraint(
        op.f("ck_jobs_pm_job_id_length"),
        "jobs",
        "pm_job_id IS NULL OR length(pm_job_id) BETWEEN 1 AND 64",
    )
    op.create_index(
        "uq_jobs_organization_id_pm_job_id",
        "jobs",
        ["organization_id", "pm_job_id"],
        unique=True,
        postgresql_where=sa.text("pm_job_id IS NOT NULL AND deleted_at IS NULL"),
    )

    op.add_column("tasks", sa.Column("pm_task_id", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("pm_linked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_tasks_pm_link"), "tasks", "(pm_task_id IS NULL) = (pm_linked_at IS NULL)"
    )
    op.create_check_constraint(
        op.f("ck_tasks_pm_task_id_length"),
        "tasks",
        "pm_task_id IS NULL OR length(pm_task_id) BETWEEN 1 AND 64",
    )
    op.create_index(
        "uq_tasks_organization_id_pm_task_id",
        "tasks",
        ["organization_id", "pm_task_id"],
        unique=True,
        postgresql_where=sa.text("pm_task_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_tasks_organization_id_pm_task_id", table_name="tasks")
    op.drop_constraint(op.f("ck_tasks_pm_task_id_length"), "tasks", type_="check")
    op.drop_constraint(op.f("ck_tasks_pm_link"), "tasks", type_="check")
    op.drop_column("tasks", "pm_linked_at")
    op.drop_column("tasks", "pm_task_id")
    op.drop_index("uq_jobs_organization_id_pm_job_id", table_name="jobs")
    op.drop_constraint(op.f("ck_jobs_pm_job_id_length"), "jobs", type_="check")
    op.drop_constraint(op.f("ck_jobs_pm_link"), "jobs", type_="check")
    op.drop_column("jobs", "pm_linked_at")
    op.drop_column("jobs", "pm_job_id")
    op.drop_table("bookkeeping_vat_mappings")
    op.drop_table("bookkeeping_service_mappings")
