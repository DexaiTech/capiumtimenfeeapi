"""period locks per month, quarter or year

A practice no longer has one "locked through" date: each month, quarter or calendar year is locked
(and unlocked) on its own, one row of ``locked_periods`` each. A lock that was set through any
date before this migration becomes one ``legacy`` row (2000-01-01 through that date), so nothing
that was locked is opened. The history gets the period an event is about.

Revision ID: 052
Revises: 051
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "052"
down_revision: str | None = "051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "locked_periods",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("period_type", sa.Text(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("locked_by", sa.UUID(), nullable=True),
        sa.Column(
            "locked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "period_type IN ('month', 'quarter', 'year', 'legacy')",
            name=op.f("ck_locked_periods_period_type"),
        ),
        sa.CheckConstraint("start_date <= end_date", name=op.f("ck_locked_periods_range")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_locked_periods_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_locked_periods")),
        sa.UniqueConstraint(
            "organization_id",
            "period_type",
            "start_date",
            name="uq_locked_periods_organization_id_period_type_start_date",
        ),
    )
    op.create_index(
        "ix_locked_periods_organization_id_dates",
        "locked_periods",
        ["organization_id", "start_date", "end_date"],
    )
    op.execute(
        """
        INSERT INTO locked_periods (id, organization_id, period_type, start_date, end_date)
        SELECT gen_random_uuid(), organization_id, 'legacy', DATE '2000-01-01', locked_through
        FROM period_locks WHERE locked_through IS NOT NULL
        """
    )
    op.execute("UPDATE period_locks SET locked_through = NULL")

    op.add_column("period_lock_events", sa.Column("period_type", sa.Text(), nullable=True))
    op.add_column("period_lock_events", sa.Column("period_start", sa.Date(), nullable=True))
    op.add_column("period_lock_events", sa.Column("period_end", sa.Date(), nullable=True))
    op.drop_constraint(
        op.f("ck_period_lock_events_locked_moves_forward"), "period_lock_events", type_="check"
    )
    op.drop_constraint(
        op.f("ck_period_lock_events_reopened_moves_back"), "period_lock_events", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_period_lock_events_period"),
        "period_lock_events",
        "period_type IS NULL OR (period_type IN ('month', 'quarter', 'year', 'legacy') "
        "AND period_start IS NOT NULL AND period_end IS NOT NULL)",
    )


def downgrade() -> None:
    # Locks of single periods cannot be a single date: the latest end of any lock is kept.
    op.execute(
        """
        UPDATE period_locks p SET locked_through =
            (SELECT max(end_date) FROM locked_periods l WHERE l.organization_id = p.organization_id)
        """
    )
    # The history is append-only: the trigger is off only while the new-style rows go.
    op.execute("ALTER TABLE period_lock_events DISABLE TRIGGER trg_period_lock_events_append_only")
    op.execute("DELETE FROM period_lock_events WHERE period_type IS NOT NULL")
    op.execute("ALTER TABLE period_lock_events ENABLE TRIGGER trg_period_lock_events_append_only")
    op.drop_constraint(op.f("ck_period_lock_events_period"), "period_lock_events", type_="check")
    op.create_check_constraint(
        op.f("ck_period_lock_events_locked_moves_forward"),
        "period_lock_events",
        "event_type <> 'locked' OR (locked_through IS NOT NULL AND "
        "(previous_locked_through IS NULL OR locked_through > previous_locked_through))",
    )
    op.create_check_constraint(
        op.f("ck_period_lock_events_reopened_moves_back"),
        "period_lock_events",
        "event_type <> 'reopened' OR (previous_locked_through IS NOT NULL AND "
        "(locked_through IS NULL OR locked_through < previous_locked_through))",
    )
    op.drop_column("period_lock_events", "period_end")
    op.drop_column("period_lock_events", "period_start")
    op.drop_column("period_lock_events", "period_type")
    op.drop_table("locked_periods")
