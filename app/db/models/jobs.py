"""Jobs: one piece of client work (a year's accounts, a quarter's VAT return, ...), its tasks
and the budget alerts it has raised."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, OrgScopedMixin, SoftDeleteMixin, TimestampMixin
from app.db.models.clients import _member_fk

JOB_STATUSES = ("planned", "in_progress", "on_hold", "completed", "closed", "cancelled")
JOB_BILLING_BASES = ("hourly", "fixed", "non_billable")
TASK_STATUSES = ("not_started", "in_progress", "on_hold", "completed")
BUDGET_ALERT_KINDS = ("minutes", "fee")
BUDGET_ALERT_THRESHOLDS = (80, 100)


class Job(Base, IdMixin, OrgScopedMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_jobs_organization_id_id"),
        # Lets time entries and invoice lines prove that their job belongs to their client.
        UniqueConstraint(
            "organization_id", "client_id", "id", name="uq_jobs_organization_id_client_id_id"
        ),
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_jobs_organization_id_client_id_clients",
        ),
        ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_jobs_organization_id_service_id_services",
        ),
        _member_fk("partner_user_id", "jobs"),
        _member_fk("manager_user_id", "jobs"),
        CheckConstraint(
            "status IN ('planned', 'in_progress', 'on_hold', 'completed', 'closed', 'cancelled')",
            name="status",
        ),
        CheckConstraint(
            "billing_basis IN ('hourly', 'fixed', 'non_billable')", name="billing_basis"
        ),
        CheckConstraint(
            "billing_basis <> 'fixed' OR fixed_fee IS NOT NULL", name="fixed_fee_required"
        ),
        CheckConstraint("fixed_fee IS NULL OR fixed_fee >= 0", name="fixed_fee"),
        CheckConstraint("hourly_rate IS NULL OR hourly_rate >= 0", name="hourly_rate"),
        CheckConstraint("budget_minutes IS NULL OR budget_minutes >= 0", name="budget_minutes"),
        CheckConstraint("budget_fee IS NULL OR budget_fee >= 0", name="budget_fee"),
        CheckConstraint("budget_cost IS NULL OR budget_cost >= 0", name="budget_cost"),
        CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_start <= period_end",
            name="period_order",
        ),
        CheckConstraint(
            "start_date IS NULL OR due_date IS NULL OR start_date <= due_date",
            name="date_order",
        ),
        CheckConstraint("closed_at IS NULL OR status = 'closed'", name="closed_at_status"),
        CheckConstraint("(pm_job_id IS NULL) = (pm_linked_at IS NULL)", name="pm_link"),
        CheckConstraint(
            "pm_job_id IS NULL OR length(pm_job_id) BETWEEN 1 AND 64", name="pm_job_id_length"
        ),
        CheckConstraint("length(name) BETWEEN 1 AND 200", name="name_length"),
        Index(
            "uq_jobs_organization_id_number",
            "organization_id",
            "number",
            unique=True,
        ),
        Index("ix_jobs_organization_id_client_id", "organization_id", "client_id"),
        Index("ix_jobs_organization_id_status_due_date", "organization_id", "status", "due_date"),
        Index("ix_jobs_organization_id_manager_user_id", "organization_id", "manager_user_id"),
        # A practice management job is linked to at most one live job of the practice.
        Index(
            "uq_jobs_organization_id_pm_job_id",
            "organization_id",
            "pm_job_id",
            unique=True,
            postgresql_where=text("pm_job_id IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # From the "job" sequence, e.g. J00042.
    number: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="planned")
    billing_basis: Mapped[str] = mapped_column(Text, nullable=False, server_default="hourly")
    fixed_fee: Mapped[int | None] = mapped_column(BigInteger)
    # Overrides every other charge-out rate for time on this job.
    hourly_rate: Mapped[int | None] = mapped_column(BigInteger)
    budget_minutes: Mapped[int | None] = mapped_column(Integer)
    budget_fee: Mapped[int | None] = mapped_column(BigInteger)
    # What the work is expected to cost at its service level, to compare with the actual cost.
    budget_cost: Mapped[int | None] = mapped_column(BigInteger)
    # The accounting period the work covers, e.g. the year ending 31 March 2026.
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    start_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    partner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    manager_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set while the job is ``closed``; a closed job takes no new time and no task changes.
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The practice management job this job is linked to, and when it was linked. A linked job's
    # name, service, manager, partner and dates belong to practice management (read-only here).
    pm_job_id: Mapped[str | None] = mapped_column(Text)
    pm_linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class JobAssignee(Base, OrgScopedMixin):
    __tablename__ = "job_assignees"
    __table_args__ = (
        PrimaryKeyConstraint("job_id", "user_id"),
        ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            name="fk_job_assignees_organization_id_job_id_jobs",
            ondelete="CASCADE",
        ),
        _member_fk("user_id", "job_assignees"),
        Index("ix_job_assignees_organization_id_user_id", "organization_id", "user_id"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class Task(Base, IdMixin, OrgScopedMixin, TimestampMixin):
    """A piece of a job's work with its own assignees, hours budget and due date."""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_tasks_organization_id_id"),
        # Lets time entries prove that their task belongs to their job.
        UniqueConstraint(
            "organization_id", "job_id", "id", name="uq_tasks_organization_id_job_id_id"
        ),
        ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            name="fk_tasks_organization_id_job_id_jobs",
            ondelete="CASCADE",
        ),
        CheckConstraint("length(title) BETWEEN 1 AND 200", name="title_length"),
        CheckConstraint(
            "description IS NULL OR length(description) <= 2000", name="description_length"
        ),
        CheckConstraint(
            "status IN ('not_started', 'in_progress', 'on_hold', 'completed')", name="status"
        ),
        CheckConstraint("budget_minutes IS NULL OR budget_minutes >= 0", name="budget_minutes"),
        CheckConstraint("sort_order >= 0", name="sort_order"),
        CheckConstraint("(status = 'completed') = (completed_at IS NOT NULL)", name="completed_at"),
        CheckConstraint("(pm_task_id IS NULL) = (pm_linked_at IS NULL)", name="pm_link"),
        CheckConstraint(
            "pm_task_id IS NULL OR length(pm_task_id) BETWEEN 1 AND 64", name="pm_task_id_length"
        ),
        Index(
            "ix_tasks_organization_id_job_id_sort_order", "organization_id", "job_id", "sort_order"
        ),
        Index("ix_tasks_organization_id_status_due_date", "organization_id", "status", "due_date"),
        Index(
            "ix_tasks_organization_id_due_date",
            "organization_id",
            "due_date",
            postgresql_where=text("due_date IS NOT NULL"),
        ),
        Index(
            "uq_tasks_organization_id_pm_task_id",
            "organization_id",
            "pm_task_id",
            unique=True,
            postgresql_where=text("pm_task_id IS NOT NULL"),
        ),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="not_started")
    budget_minutes: Mapped[int | None] = mapped_column(Integer)
    due_date: Mapped[date | None] = mapped_column(Date)
    # Position in the job's task list (0 first).
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The practice management task this task is linked to (only on a linked job).
    pm_task_id: Mapped[str | None] = mapped_column(Text)
    pm_linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class TaskAssignee(Base, OrgScopedMixin):
    __tablename__ = "task_assignees"
    __table_args__ = (
        PrimaryKeyConstraint("task_id", "user_id"),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_task_assignees_organization_id_task_id_tasks",
            ondelete="CASCADE",
        ),
        _member_fk("user_id", "task_assignees"),
        Index("ix_task_assignees_organization_id_user_id", "organization_id", "user_id"),
    )

    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class JobBudgetAlert(Base, IdMixin, OrgScopedMixin):
    """A budget threshold a job has crossed and is still above.

    One row per job, kind (``minutes`` or ``fee``) and threshold (80 or 100 per cent): its
    existence is what stops the alert being raised twice. The row goes when usage falls back
    below the threshold, so crossing it again raises the alert again.
    """

    __tablename__ = "job_budget_alerts"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "job_id",
            "kind",
            "threshold",
            name="uq_job_budget_alerts_organization_id_job_id_kind_threshold",
        ),
        ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["jobs.organization_id", "jobs.id"],
            name="fk_job_budget_alerts_organization_id_job_id_jobs",
            ondelete="CASCADE",
        ),
        CheckConstraint("kind IN ('minutes', 'fee')", name="kind"),
        CheckConstraint("threshold IN (80, 100)", name="threshold"),
        CheckConstraint("usage_bp >= 0", name="usage_bp"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    threshold: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Usage in basis points (10000 = 100%) when the threshold was crossed.
    usage_bp: Mapped[int] = mapped_column(BigInteger, nullable=False)


__all__ = ["Job", "JobAssignee", "JobBudgetAlert", "Task", "TaskAssignee"]
