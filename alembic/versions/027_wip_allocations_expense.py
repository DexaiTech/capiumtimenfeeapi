"""work in progress: expense lines can be allocated to invoices and write-offs

``wip_allocations.expense_id`` points at an approved expense claim line (``expenses``): the third
kind of WIP item next to time entries and disbursements. Exactly one of the three item columns
is set, and a live (not reversed) allocation per expense line is unique, as for the other two.

Revision ID: 027
Revises: 026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "027"
down_revision: str | None = "026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("wip_allocations", sa.Column("expense_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_wip_allocations_expense",
        "wip_allocations",
        "expenses",
        ["organization_id", "expense_id"],
        ["organization_id", "id"],
    )
    op.drop_constraint(op.f("ck_wip_allocations_one_item"), "wip_allocations", type_="check")
    op.create_check_constraint(
        op.f("ck_wip_allocations_one_item"),
        "wip_allocations",
        "num_nonnulls(time_entry_id, disbursement_id, expense_id) = 1",
    )
    op.create_index(
        "uq_wip_allocations_expense_live",
        "wip_allocations",
        ["expense_id"],
        unique=True,
        postgresql_where=sa.text("expense_id IS NOT NULL AND reversed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_wip_allocations_expense_live", table_name="wip_allocations")
    op.execute("DELETE FROM wip_allocations WHERE expense_id IS NOT NULL")
    op.drop_constraint(op.f("ck_wip_allocations_one_item"), "wip_allocations", type_="check")
    op.create_check_constraint(
        op.f("ck_wip_allocations_one_item"),
        "wip_allocations",
        "(time_entry_id IS NULL) <> (disbursement_id IS NULL)",
    )
    op.drop_constraint("fk_wip_allocations_expense", "wip_allocations", type_="foreignkey")
    op.drop_column("wip_allocations", "expense_id")
