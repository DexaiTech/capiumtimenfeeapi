"""time that needs no approval

Adds the practice setting ``auto_approve_privileged_time`` (on by default), the permission
``timesheet.exempt`` (Owner, Admin and Partner hold it) and ``timesheets.approval_exempt``: a sheet
of someone who holds the permission counts as approved for work in progress and billing without
anybody approving it. Existing sheets of those people are marked straight away.

Revision ID: 053
Revises: 052
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import sync_permissions

revision: str = "053"
down_revision: str | None = "052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GRANTED_TO = ("owner", "admin", "partner")


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "auto_approve_privileged_time",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column(
        "timesheets",
        sa.Column("approval_exempt", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    sync_permissions()
    roles = ", ".join(f"'{key}'" for key in GRANTED_TO)
    op.execute(
        f"""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r JOIN permissions p ON p.code = 'timesheet.exempt'
        WHERE r.system_key IN ({roles})
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE timesheets t SET approval_exempt = true
        FROM organizations o
        WHERE o.id = t.organization_id AND o.auto_approve_privileged_time
          AND EXISTS (
            SELECT 1 FROM organization_members m
            JOIN role_permissions rp ON rp.role_id = m.role_id
            JOIN permissions p ON p.id = rp.permission_id AND p.code = 'timesheet.exempt'
            WHERE m.organization_id = t.organization_id AND m.user_id = t.user_id
              AND m.status = 'active')
        """
    )


def downgrade() -> None:
    op.drop_column("timesheets", "approval_exempt")
    op.drop_column("organizations", "auto_approve_privileged_time")
    op.execute("DELETE FROM permissions WHERE code = 'timesheet.exempt'")
