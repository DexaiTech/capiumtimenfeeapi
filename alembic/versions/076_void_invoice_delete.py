"""void invoice delete

Revision ID: 076
Revises: 075

Ticket APT71: a void invoice can be deleted. The delete is soft: ``invoices.deleted_at`` and
``deleted_by`` are set and the invoice leaves the invoice list, its status counts and the client's
figures, but the row, its number (invoice numbers stay gapless for VAT), its lines and its audit
trail are kept. Only a void invoice can carry ``deleted_at``; drafts are still deleted outright.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "076"
down_revision: str | None = "075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("invoices", sa.Column("deleted_by", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_invoices_deleted_by_users"), "invoices", "users", ["deleted_by"], ["id"]
    )
    op.create_check_constraint(
        op.f("ck_invoices_deleted_only_void"),
        "invoices",
        "deleted_at IS NULL OR status = 'void'",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_invoices_deleted_only_void"), "invoices", type_="check")
    op.drop_constraint(op.f("fk_invoices_deleted_by_users"), "invoices", type_="foreignkey")
    op.drop_column("invoices", "deleted_by")
    op.drop_column("invoices", "deleted_at")
