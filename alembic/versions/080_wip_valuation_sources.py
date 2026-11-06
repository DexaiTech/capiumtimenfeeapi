"""wip valuation sources

Revision ID: 080
Revises: 079

Ticket APT84: the practice chooses which rate sources value its time, and in what order.

* ``organizations.wip_valuation_sources``: the enabled sources, first to last, of ``client`` (the
  client's rates and the job's own rate), ``service``, ``grade`` and ``staff``; 1 to 4, each at
  most once. Each practice gets the order of its rate mode, so no value changes: ``staff_first``
  -> client, service, staff, grade; ``grade_only`` -> client, service, grade; ``staff_only`` ->
  client, service, staff. A new practice starts with client, service, grade, staff.
* ``organizations.rate_mode`` is dropped: the API derives it from the sources.
* ``time_entries.rate_source`` may be ``none``: no enabled source had a rate and the practice
  default is 0 ("No rate").

Downgrading gives each practice the rate mode closest to its sources and records ``none`` as
``default`` again (both valued at 0).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "080"
down_revision: str | None = "079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCES_CHECK = (
    "cardinality(wip_valuation_sources) BETWEEN 1 AND 4 "
    "AND wip_valuation_sources <@ ARRAY['client', 'service', 'grade', 'staff']::text[] "
    "AND cardinality(array_positions(wip_valuation_sources, 'client')) <= 1 "
    "AND cardinality(array_positions(wip_valuation_sources, 'service')) <= 1 "
    "AND cardinality(array_positions(wip_valuation_sources, 'grade')) <= 1 "
    "AND cardinality(array_positions(wip_valuation_sources, 'staff')) <= 1"
)
_OLD_SOURCES = (
    "'client_service_staff', 'client_service', 'client_staff', 'client', 'job', "
    "'service_staff', 'service_grade', 'service', 'staff', 'grade', 'default'"
)
_NEW_SOURCES = _OLD_SOURCES + ", 'none'"


def _rate_source_check(sources: str) -> None:
    op.drop_constraint(op.f("ck_time_entries_rate_source"), "time_entries", type_="check")
    op.create_check_constraint(
        op.f("ck_time_entries_rate_source"),
        "time_entries",
        f"rate_source IS NULL OR rate_source IN ({sources})",
    )


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "wip_valuation_sources",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{client,service,grade,staff}'::text[]"),
        ),
    )
    op.execute(
        "UPDATE organizations SET wip_valuation_sources = CASE rate_mode "
        "WHEN 'grade_only' THEN '{client,service,grade}'::text[] "
        "WHEN 'staff_only' THEN '{client,service,staff}'::text[] "
        "ELSE '{client,service,staff,grade}'::text[] END"
    )
    op.create_check_constraint(
        op.f("ck_organizations_wip_valuation_sources"), "organizations", _SOURCES_CHECK
    )
    op.drop_constraint(op.f("ck_organizations_rate_mode"), "organizations", type_="check")
    op.drop_column("organizations", "rate_mode")
    _rate_source_check(_NEW_SOURCES)


def downgrade() -> None:
    op.execute("UPDATE time_entries SET rate_source = 'default' WHERE rate_source = 'none'")
    _rate_source_check(_OLD_SOURCES)
    op.add_column(
        "organizations",
        sa.Column("rate_mode", sa.Text(), server_default="staff_first", nullable=False),
    )
    op.execute(
        "UPDATE organizations SET rate_mode = CASE "
        "WHEN 'grade' = ANY(wip_valuation_sources) AND NOT 'staff' = ANY(wip_valuation_sources) "
        "THEN 'grade_only' "
        "WHEN 'staff' = ANY(wip_valuation_sources) AND NOT 'grade' = ANY(wip_valuation_sources) "
        "THEN 'staff_only' "
        "ELSE 'staff_first' END"
    )
    op.create_check_constraint(
        op.f("ck_organizations_rate_mode"),
        "organizations",
        "rate_mode IN ('staff_first', 'grade_only', 'staff_only')",
    )
    op.drop_constraint(
        op.f("ck_organizations_wip_valuation_sources"), "organizations", type_="check"
    )
    op.drop_column("organizations", "wip_valuation_sources")
