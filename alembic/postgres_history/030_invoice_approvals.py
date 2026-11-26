"""invoice approval: the awaiting_approval and approved statuses and their history

An invoice can now be ``awaiting_approval`` or ``approved`` before it is issued: like a draft it has
no number and may have no dates yet. ``invoice_approvals`` is the append-only history of the
submit / approve / reject steps.

Revision ID: 030
Revises: 029
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "030"
down_revision: str | None = "029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNISSUED = "'draft', 'awaiting_approval', 'approved'"


def _invoice_checks(statuses: str, unissued: str) -> None:
    op.create_check_constraint(op.f("ck_invoices_status"), "invoices", f"status IN ({statuses})")
    op.create_check_constraint(
        op.f("ck_invoices_number_when_issued"),
        "invoices",
        f"(status IN ({unissued})) = (number IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_invoices_dates_when_issued"),
        "invoices",
        f"status IN ({unissued}) OR (issue_date IS NOT NULL AND due_date IS NOT NULL)",
    )


def _drop_invoice_checks() -> None:
    for name in (
        "ck_invoices_dates_when_issued",
        "ck_invoices_number_when_issued",
        "ck_invoices_status",
    ):
        op.drop_constraint(op.f(name), "invoices", type_="check")


def upgrade() -> None:
    _drop_invoice_checks()
    _invoice_checks(f"{UNISSUED}, 'issued', 'void'", UNISSUED)

    op.create_table(
        "invoice_approvals",
        sa.Column("invoice_id", sa.UUID(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.UUID(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("gross_total", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "action IN ('submitted', 'approved', 'rejected')",
            name=op.f("ck_invoice_approvals_action"),
        ),
        sa.CheckConstraint(
            "note IS NULL OR length(note) BETWEEN 1 AND 500",
            name=op.f("ck_invoice_approvals_note_length"),
        ),
        sa.CheckConstraint(
            "action <> 'rejected' OR note IS NOT NULL",
            name=op.f("ck_invoice_approvals_rejection_has_reason"),
        ),
        sa.CheckConstraint("gross_total >= 0", name=op.f("ck_invoice_approvals_gross_total")),
        sa.ForeignKeyConstraint(
            ["organization_id", "invoice_id"],
            ["invoices.organization_id", "invoices.id"],
            name=op.f("fk_invoice_approvals_invoice"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name=op.f("fk_invoice_approvals_organization_id_actor_members"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_invoice_approvals_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_approvals")),
    )
    op.create_index(
        "ix_invoice_approvals_invoice_id_created_at",
        "invoice_approvals",
        ["invoice_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("invoice_approvals")
    # Invoices waiting for or past approval go back to drafts, as they were before this release.
    op.execute(
        "UPDATE invoices SET status = 'draft' WHERE status IN ('awaiting_approval', 'approved')"
    )
    _drop_invoice_checks()
    _invoice_checks("'draft', 'issued', 'void'", "'draft'")
