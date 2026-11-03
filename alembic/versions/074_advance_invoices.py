"""advance invoices

Revision ID: 074
Revises: 073

Ticket APT69: fees billed in advance, with the time assigned to them later. A manual invoice line
can be marked ``invoice_lines.is_advance``; once the invoice is issued, unbilled WIP of the same
client is assigned to that line as ordinary WIP allocations, without changing the invoice. The
allocation keeps the day it was assigned (``wip_allocations.assigned_on``) so WIP as at an earlier
day still counts the work, although the invoice is dated before it. Both columns are new, so the
downgrade simply drops them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "074"
down_revision: str | None = "073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoice_lines",
        sa.Column("is_advance", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_invoice_lines_advance_is_manual"),
        "invoice_lines",
        "NOT is_advance OR kind = 'manual'",
    )
    op.add_column("wip_allocations", sa.Column("assigned_on", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("wip_allocations", "assigned_on")
    op.drop_constraint(op.f("ck_invoice_lines_advance_is_manual"), "invoice_lines", type_="check")
    op.drop_column("invoice_lines", "is_advance")
