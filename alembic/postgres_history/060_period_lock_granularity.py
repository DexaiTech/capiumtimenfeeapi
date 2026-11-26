"""period lock granularity and accounting year

Revision ID: 060
Revises: 059

A practice chooses once how it locks: ``weekly`` (the default), ``monthly``, ``quarterly`` or
``annually``, and the day its accounting year starts (``financial_year_start_month`` and
``_day``; 1 January keeps the calendar year every practice had). New locks must be exactly one
period of that length inside the accounting year, so a lock row can now be a ``week`` too.
``period_lock_configured`` is false until the choice is made; a practice that already has locks
starts with the length it used last, so nothing it locked changes and it is not asked again.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "060"
down_revision: str | None = "059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_TYPES = "'month', 'quarter', 'year', 'legacy'"
NEW_TYPES = "'week', 'month', 'quarter', 'year', 'legacy'"


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("period_lock_granularity", sa.Text(), nullable=False, server_default="weekly"),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "financial_year_start_month", sa.SmallInteger(), nullable=False, server_default="1"
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "financial_year_start_day", sa.SmallInteger(), nullable=False, server_default="1"
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "period_lock_configured", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_check_constraint(
        op.f("ck_organizations_period_lock_granularity"),
        "organizations",
        "period_lock_granularity IN ('weekly', 'monthly', 'quarterly', 'annually')",
    )
    op.create_check_constraint(
        op.f("ck_organizations_financial_year_start"),
        "organizations",
        "financial_year_start_month BETWEEN 1 AND 12 AND financial_year_start_day BETWEEN 1 AND 28",
    )
    op.execute(
        """
        UPDATE organizations o SET period_lock_configured = true,
            period_lock_granularity = COALESCE((
                SELECT CASE l.period_type WHEN 'quarter' THEN 'quarterly'
                                          WHEN 'year' THEN 'annually' ELSE 'monthly' END
                FROM locked_periods l WHERE l.organization_id = o.id
                ORDER BY l.locked_at DESC, l.id LIMIT 1
            ), 'monthly')
        WHERE EXISTS (SELECT 1 FROM locked_periods l WHERE l.organization_id = o.id)
           OR EXISTS (SELECT 1 FROM period_lock_events e WHERE e.organization_id = o.id)
        """
    )

    op.drop_constraint(op.f("ck_locked_periods_period_type"), "locked_periods", type_="check")
    op.create_check_constraint(
        op.f("ck_locked_periods_period_type"), "locked_periods", f"period_type IN ({NEW_TYPES})"
    )
    op.drop_constraint(op.f("ck_period_lock_events_period"), "period_lock_events", type_="check")
    op.create_check_constraint(
        op.f("ck_period_lock_events_period"),
        "period_lock_events",
        f"period_type IS NULL OR (period_type IN ({NEW_TYPES}) "
        "AND period_start IS NOT NULL AND period_end IS NOT NULL)",
    )


def downgrade() -> None:
    # Weekly locks do not exist before this migration: they and their history go. The history is
    # append-only, so the trigger is off only while those rows are removed.
    op.execute("DELETE FROM locked_periods WHERE period_type = 'week'")
    op.execute("ALTER TABLE period_lock_events DISABLE TRIGGER trg_period_lock_events_append_only")
    op.execute("DELETE FROM period_lock_events WHERE period_type = 'week'")
    op.execute("ALTER TABLE period_lock_events ENABLE TRIGGER trg_period_lock_events_append_only")
    op.drop_constraint(op.f("ck_period_lock_events_period"), "period_lock_events", type_="check")
    op.create_check_constraint(
        op.f("ck_period_lock_events_period"),
        "period_lock_events",
        f"period_type IS NULL OR (period_type IN ({OLD_TYPES}) "
        "AND period_start IS NOT NULL AND period_end IS NOT NULL)",
    )
    op.drop_constraint(op.f("ck_locked_periods_period_type"), "locked_periods", type_="check")
    op.create_check_constraint(
        op.f("ck_locked_periods_period_type"), "locked_periods", f"period_type IN ({OLD_TYPES})"
    )
    op.drop_constraint(
        op.f("ck_organizations_financial_year_start"), "organizations", type_="check"
    )
    op.drop_constraint(
        op.f("ck_organizations_period_lock_granularity"), "organizations", type_="check"
    )
    op.drop_column("organizations", "period_lock_configured")
    op.drop_column("organizations", "financial_year_start_day")
    op.drop_column("organizations", "financial_year_start_month")
    op.drop_column("organizations", "period_lock_granularity")
