"""tasks, the closed job status and job budget alerts

Adds ``tasks`` and ``task_assignees`` (the work inside a job), ``job_budget_alerts`` (the 80% and
100% budget thresholds a job has crossed and is still above), the ``closed`` job status with
``jobs.closed_at``, ``time_entries.task_id`` and ``timers.task_id`` (the task time is logged
against; it must belong to the same job), and a check that a job does not start after it is due.
Existing jobs are untouched; a job saved before the API checked the date order keeps its dates
(the check is added ``NOT VALID``, so it applies to new and changed rows only).

Revision ID: 021
Revises: 020
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers, timestamps

revision: str = "021"
down_revision: str | None = "020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_JOB_STATUSES = "'planned', 'in_progress', 'on_hold', 'completed', 'cancelled'"
NEW_JOB_STATUSES = "'planned', 'in_progress', 'on_hold', 'completed', 'closed', 'cancelled'"
TASK_STATUSES = "'not_started', 'in_progress', 'on_hold', 'completed'"
DATE_ORDER = "start_date IS NULL OR due_date IS NULL OR start_date <= due_date"
# Time and timers carry the task they are logged against; it must belong to their job.
TASK_LINKED_TABLES = ("time_entries", "timers")


def _organization_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.add_column("jobs", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint(op.f("ck_jobs_status"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_status"), "jobs", f"status IN ({NEW_JOB_STATUSES})")
    op.create_check_constraint(
        op.f("ck_jobs_closed_at_status"), "jobs", "closed_at IS NULL OR status = 'closed'"
    )
    op.execute(f"ALTER TABLE jobs ADD CONSTRAINT ck_jobs_date_order CHECK ({DATE_ORDER}) NOT VALID")

    op.create_table(
        "tasks",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="not_started", nullable=False),
        sa.Column("budget_minutes", sa.Integer(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *timestamps(),
        sa.CheckConstraint("length(title) BETWEEN 1 AND 200", name=op.f("ck_tasks_title_length")),
        sa.CheckConstraint(
            "description IS NULL OR length(description) <= 2000",
            name=op.f("ck_tasks_description_length"),
        ),
        sa.CheckConstraint(f"status IN ({TASK_STATUSES})", name=op.f("ck_tasks_status")),
        sa.CheckConstraint(
            "budget_minutes IS NULL OR budget_minutes >= 0", name=op.f("ck_tasks_budget_minutes")
        ),
        sa.CheckConstraint("sort_order >= 0", name=op.f("ck_tasks_sort_order")),
        sa.CheckConstraint(
            "(status = 'completed') = (completed_at IS NOT NULL)",
            name=op.f("ck_tasks_completed_at"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_tasks_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            name="fk_tasks_organization_id_job_id_jobs",
            ondelete="CASCADE",
        ),
        _organization_fk("tasks"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tasks")),
        sa.UniqueConstraint("organization_id", "id", name="uq_tasks_organization_id_id"),
        sa.UniqueConstraint(
            "organization_id", "job_id", "id", name="uq_tasks_organization_id_job_id_id"
        ),
    )
    op.create_index(
        "ix_tasks_organization_id_job_id_sort_order",
        "tasks",
        ["organization_id", "job_id", "sort_order"],
        unique=False,
    )
    op.create_index(
        "ix_tasks_organization_id_status_due_date",
        "tasks",
        ["organization_id", "status", "due_date"],
        unique=False,
    )
    op.create_index(
        "ix_tasks_organization_id_due_date",
        "tasks",
        ["organization_id", "due_date"],
        unique=False,
        postgresql_where=sa.text("due_date IS NOT NULL"),
    )
    add_updated_at_triggers("tasks")

    op.create_table(
        "task_assignees",
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_task_assignees_organization_id_task_id_tasks",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_task_assignees_organization_id_user_id_organization_members",
        ),
        _organization_fk("task_assignees"),
        sa.PrimaryKeyConstraint("task_id", "user_id", name=op.f("pk_task_assignees")),
    )
    op.create_index(
        "ix_task_assignees_organization_id_user_id",
        "task_assignees",
        ["organization_id", "user_id"],
        unique=False,
    )

    op.create_table(
        "job_budget_alerts",
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("threshold", sa.SmallInteger(), nullable=False),
        sa.Column(
            "triggered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("usage_bp", sa.BigInteger(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint("kind IN ('minutes', 'fee')", name=op.f("ck_job_budget_alerts_kind")),
        sa.CheckConstraint("threshold IN (80, 100)", name=op.f("ck_job_budget_alerts_threshold")),
        sa.CheckConstraint("usage_bp >= 0", name=op.f("ck_job_budget_alerts_usage_bp")),
        sa.ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            name="fk_job_budget_alerts_organization_id_job_id_jobs",
            ondelete="CASCADE",
        ),
        _organization_fk("job_budget_alerts"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_budget_alerts")),
        sa.UniqueConstraint(
            "organization_id",
            "job_id",
            "kind",
            "threshold",
            name="uq_job_budget_alerts_organization_id_job_id_kind_threshold",
        ),
    )

    # The task a piece of time is logged against: it must belong to the entry's job.
    for table in TASK_LINKED_TABLES:
        op.add_column(table, sa.Column("task_id", sa.UUID(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_organization_id_job_id_task_id_tasks",
            table,
            "tasks",
            ["organization_id", "job_id", "task_id"],
            ["organization_id", "job_id", "id"],
        )
        # A composite foreign key is not enforced while one of its columns is null.
        op.create_check_constraint(
            op.f(f"ck_{table}_task_needs_job"), table, "task_id IS NULL OR job_id IS NOT NULL"
        )
    op.create_index(
        "ix_time_entries_organization_id_task_id",
        "time_entries",
        ["organization_id", "task_id"],
        unique=False,
        postgresql_where=sa.text("task_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Everything is dropped "if exists": a database that recorded 021 while it was an empty
    # placeholder has none of it, and must still be able to downgrade past it.
    op.execute("DROP INDEX IF EXISTS ix_time_entries_organization_id_task_id")
    for table in TASK_LINKED_TABLES:
        # Dropping the column drops its foreign key and check too.
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS task_id")
    op.execute("DROP TABLE IF EXISTS job_budget_alerts")
    op.execute("DROP TABLE IF EXISTS task_assignees")
    op.execute("DROP TABLE IF EXISTS tasks")

    # A closed job goes back to completed: it was finished, and the old status list has no
    # closed. It loses its closing time.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'jobs' AND column_name = 'closed_at'
            ) THEN
                UPDATE jobs SET status = 'completed',
                    completed_at = COALESCE(completed_at, closed_at, now()), closed_at = NULL
                WHERE status = 'closed';
            END IF;
        END $$
        """
    )
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS ck_jobs_date_order")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS ck_jobs_closed_at_status")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS ck_jobs_status")
    op.create_check_constraint(op.f("ck_jobs_status"), "jobs", f"status IN ({OLD_JOB_STATUSES})")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS closed_at")
