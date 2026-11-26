"""practice settings for expenses, WIP and invoice approval, and the permissions of the later modules

The practice gets the thresholds and rates the expense, WIP and billing modules read (receipt and
WIP-adjustment thresholds, invoice approval, mileage rate, expense markup, how a fixed fee counts
as WIP). New practices default to a 6-minute time increment. The permission catalogue grows by the
codes of period locks, expenses, WIP adjustments, invoice approval, estimates, recurring invoices,
opening balances, exports, integrations and data tools; existing practices get them on their
system roles (new practices copy the roles from the catalogue).

Revision ID: 019
Revises: 018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import sync_permissions

revision: str = "019"
down_revision: str | None = "018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_PERMISSIONS = (
    "period_lock.read",
    "period_lock.manage",
    "period_lock.reopen",
    "expense.log",
    "expense.read_all",
    "expense.approve",
    "expense.manage",
    "wip.adjust",
    "wip.approve",
    "wip.transfer",
    "invoice.approve",
    "estimate.read",
    "estimate.manage",
    "recurring.read",
    "recurring.manage",
    "opening_balance.import",
    "report.export",
    "audit_log.export",
    "integration.read",
    "integration.manage",
    "data.import_export",
    "onboarding.manage",
)
_NOT_PARTNER = ("period_lock.reopen", "integration.manage", "data.import_export")
# What each system role of every existing practice is granted.
GRANTS: dict[str, tuple[str, ...]] = {
    "owner": NEW_PERMISSIONS,
    "admin": NEW_PERMISSIONS,
    "partner": tuple(code for code in NEW_PERMISSIONS if code not in _NOT_PARTNER),
    "manager": (
        "period_lock.read",
        "expense.log",
        "expense.read_all",
        "expense.approve",
        "wip.adjust",
        "wip.transfer",
        "estimate.read",
        "estimate.manage",
        "recurring.read",
        "report.export",
    ),
    "staff": ("expense.log",),
    "billing": (
        "period_lock.read",
        "expense.read_all",
        "estimate.read",
        "estimate.manage",
        "recurring.read",
        "recurring.manage",
        "opening_balance.import",
        "report.export",
    ),
    "viewer": ("estimate.read", "recurring.read"),
}

_CHECKS = (
    ("fixed_fee_wip_mode", "fixed_fee_wip_mode IN ('on_completion', 'by_budget_progress')"),
    ("wip_adjustment_approval_threshold", "wip_adjustment_approval_threshold >= 0"),
    ("receipt_required_threshold", "receipt_required_threshold >= 0"),
    ("invoice_approval_threshold", "invoice_approval_threshold >= 0"),
    ("mileage_rate", "mileage_rate >= 0"),
    ("expense_markup_bp", "expense_markup_bp BETWEEN 0 AND 100000"),
)


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("fixed_fee_wip_mode", sa.Text(), server_default="on_completion", nullable=False),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "wip_adjustment_approval_threshold",
            sa.BigInteger(),
            server_default=sa.text("50000"),
            nullable=False,
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "receipt_required_threshold",
            sa.BigInteger(),
            server_default=sa.text("2500"),
            nullable=False,
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "invoice_approval_required",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "invoice_approval_threshold",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "organizations",
        sa.Column("mileage_rate", sa.BigInteger(), server_default=sa.text("45"), nullable=False),
    )
    op.add_column(
        "organizations",
        sa.Column("expense_markup_bp", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    for name, condition in _CHECKS:
        op.create_check_constraint(op.f(f"ck_organizations_{name}"), "organizations", condition)
    # New practices only: the stored value of an existing practice is left as it is.
    op.alter_column("organizations", "time_increment_minutes", server_default=sa.text("6"))

    # The system roles of every existing practice (new practices copy them from the catalogue).
    sync_permissions()
    grants = ", ".join(f"('{role}', '{code}')" for role, codes in GRANTS.items() for code in codes)
    op.execute(
        f"""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id
        FROM roles r
        JOIN (VALUES {grants}) AS g(system_key, code) ON g.system_key = r.system_key
        JOIN permissions p ON p.code = g.code
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    # Deleting a permission removes its grants (role_permissions cascades).
    codes = ", ".join(f"'{code}'" for code in NEW_PERMISSIONS)
    op.execute(f"DELETE FROM permissions WHERE code IN ({codes})")

    op.alter_column("organizations", "time_increment_minutes", server_default=sa.text("1"))
    for name, _condition in reversed(_CHECKS):
        op.drop_constraint(op.f(f"ck_organizations_{name}"), "organizations", type_="check")
    for column in (
        "expense_markup_bp",
        "mileage_rate",
        "invoice_approval_threshold",
        "invoice_approval_required",
        "receipt_required_threshold",
        "wip_adjustment_approval_threshold",
        "fixed_fee_wip_mode",
    ):
        op.drop_column("organizations", column)
