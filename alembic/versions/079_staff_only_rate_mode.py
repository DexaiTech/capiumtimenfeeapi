"""staff only rate mode

Revision ID: 079
Revises: 077

Ticket APT79: a practice whose staff rates differ from the grade rates can price time by staff
rates only (``organizations.rate_mode = 'staff_only'``): the grade rates are skipped and a person
without a rate of their own is charged the practice default. Allows the new value in the rate mode
check. Downgrading turns it into ``staff_first`` (a person's own rate still comes first).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "079"
down_revision: str | None = "077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_organizations_rate_mode"), "organizations", type_="check")
    op.create_check_constraint(
        op.f("ck_organizations_rate_mode"),
        "organizations",
        "rate_mode IN ('staff_first', 'grade_only', 'staff_only')",
    )


def downgrade() -> None:
    op.execute("UPDATE organizations SET rate_mode = 'staff_first' WHERE rate_mode = 'staff_only'")
    op.drop_constraint(op.f("ck_organizations_rate_mode"), "organizations", type_="check")
    op.create_check_constraint(
        op.f("ck_organizations_rate_mode"),
        "organizations",
        "rate_mode IN ('staff_first', 'grade_only')",
    )
