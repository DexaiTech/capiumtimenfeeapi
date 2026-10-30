"""fill missing time costs

Revision ID: 064
Revises: 063

Data only (ticket APT51). Time recorded before its person's cost rate existed, or dated before
the day that rate starts, was saved with a cost of 0, so the billing summary, job budgets, WIP
and reports showed an actual cost of nothing for it. The cost ladder now also costs time dated
before a person's first cost rate at that rate, and adding a cost rate fills in missing costs
(``app/services/time_costs.py``); this does the same once for the time already recorded.

Exactly as the application does it: only unbilled entries with a cost rate of 0 outside every
locked period, priced by the cost ladder of ``app/services/charge_rates.py`` (the person's staff
cost rule in force, else their grade's cost rule in force, else the grade's default cost rate,
else their first staff cost rule when the entry is dated before it), times the overtime
multiplier for overtime time in a practice that has overtime on, rounded half up. The
charge-out rate and value are never touched. Entries that still come to 0 stay as they are, so
running it again changes nothing.

Downgrade does nothing: a filled-in cost cannot be told apart from one saved with the entry.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "064"
down_revision: str | None = "063"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A rule in force on the entry's day.
_IN_FORCE = (
    "r.organization_id = te.organization_id AND r.kind = 'cost' AND r.client_id IS NULL "
    "AND r.service_id IS NULL AND r.effective_from <= te.entry_date "
    "AND (r.effective_to IS NULL OR te.entry_date <= r.effective_to)"
)


def upgrade() -> None:
    op.execute(
        f"""
        WITH first_own AS (
            SELECT DISTINCT ON (organization_id, user_id)
                organization_id, user_id, rate, effective_from
            FROM rate_rules
            WHERE kind = 'cost' AND user_id IS NOT NULL AND client_id IS NULL
              AND service_id IS NULL AND grade_id IS NULL
            ORDER BY organization_id, user_id, effective_from
        ),
        priced AS (
            SELECT
                te.id,
                te.minutes,
                COALESCE(
                    (SELECT r.rate FROM rate_rules r
                     WHERE {_IN_FORCE} AND r.user_id = te.user_id AND r.grade_id IS NULL
                     LIMIT 1),
                    (SELECT r.rate FROM rate_rules r
                     WHERE {_IN_FORCE} AND r.grade_id = m.grade_id AND r.user_id IS NULL
                     LIMIT 1),
                    g.default_cost_rate,
                    CASE WHEN fo.effective_from > te.entry_date THEN fo.rate END,
                    0
                ) AS base_rate,
                CASE
                    WHEN te.is_overtime AND o.overtime_enabled
                    THEN COALESCE(m.overtime_multiplier_bp, o.overtime_multiplier_bp)
                END AS overtime_bp
            FROM time_entries te
            JOIN organizations o ON o.id = te.organization_id
            JOIN organization_members m
              ON m.organization_id = te.organization_id AND m.user_id = te.user_id
            LEFT JOIN grades g ON g.organization_id = m.organization_id AND g.id = m.grade_id
            LEFT JOIN first_own fo
              ON fo.organization_id = te.organization_id AND fo.user_id = te.user_id
            WHERE te.cost_rate = 0
              AND te.billing_status = 'unbilled'
              AND NOT EXISTS (
                  SELECT 1 FROM locked_periods lp
                  WHERE lp.organization_id = te.organization_id
                    AND te.entry_date BETWEEN lp.start_date AND lp.end_date
              )
        ),
        rated AS (
            SELECT
                id,
                minutes,
                CASE
                    WHEN overtime_bp IS NULL THEN base_rate
                    ELSE (base_rate * overtime_bp + 5000) / 10000
                END AS cost_rate
            FROM priced
        )
        UPDATE time_entries te
        SET cost_rate = rated.cost_rate,
            cost_value = (rated.minutes * rated.cost_rate + 30) / 60
        FROM rated
        WHERE te.id = rated.id AND rated.cost_rate > 0
        """
    )


def downgrade() -> None:
    pass
