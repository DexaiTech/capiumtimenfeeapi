"""approval switches

Revision ID: 067
Revises: 066

Ticket APT53: a practice can switch each approval flow off. Timesheets and invoices already had
a switch (``require_timesheet_approval``, ``invoice_approval_required``); this adds the two
missing ones, both on so nothing changes for a practice until it switches them off:

* ``organizations.expense_approval_required``: off, a submitted expense claim is approved at once;
* ``organizations.wip_adjustment_approval_required``: off, every WIP write-on and write-off takes
  effect at once, whatever its amount.

Timesheet approval switched off now means that nobody approves time at all: every sheet of the
practice is ``approval_exempt``, as the sheets of people who hold ``timesheet.exempt`` already
were. The flag is kept by the application when the switch changes; this sets it once for the
practices that have timesheet approval off today. Sheets already waiting for approval in those
practices are left as they are, for an approver to decide.

Downgrade drops the two columns and gives back the exemptions of the practices with timesheet
approval off to the people who hold the permission (the application recomputes them anyway on
the next write).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "067"
down_revision: str | None = "066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "expense_approval_required", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "wip_adjustment_approval_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.execute(
        "UPDATE timesheets t SET approval_exempt = true FROM organizations o "
        "WHERE o.id = t.organization_id AND NOT o.require_timesheet_approval "
        "AND NOT t.approval_exempt"
    )


def downgrade() -> None:
    # Back to the rule before 067: exempt only when the practice setting is on and the person's
    # role (or a grant of their own, not denied) holds ``timesheet.exempt``.
    op.execute(
        """
        UPDATE timesheets t SET approval_exempt = (
            o.auto_approve_privileged_time AND EXISTS (
                SELECT 1 FROM organization_members m
                WHERE m.organization_id = t.organization_id AND m.user_id = t.user_id
                AND m.status = 'active' AND (
                    EXISTS (
                        SELECT 1 FROM role_permissions rp
                        JOIN permissions p ON p.id = rp.permission_id
                        WHERE rp.role_id = m.role_id AND p.code = 'timesheet.exempt'
                    ) OR EXISTS (
                        SELECT 1 FROM member_permission_overrides g
                        WHERE g.organization_id = t.organization_id AND g.user_id = t.user_id
                        AND g.permission_code = 'timesheet.exempt' AND g.effect = 'grant'
                    )
                )
            ) AND NOT EXISTS (
                SELECT 1 FROM member_permission_overrides d
                WHERE d.organization_id = t.organization_id AND d.user_id = t.user_id
                AND d.permission_code = 'timesheet.exempt' AND d.effect = 'deny'
            )
        )
        FROM organizations o
        WHERE o.id = t.organization_id AND NOT o.require_timesheet_approval
        """
    )
    op.drop_column("organizations", "wip_adjustment_approval_required")
    op.drop_column("organizations", "expense_approval_required")
