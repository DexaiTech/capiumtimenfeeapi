"""deleted staff status

Revision ID: 070
Revises: 069

Ticket APT62: a staff member who is deleted while time, expenses, invoices or other records refer
to them is kept with ``organization_members.status = 'deleted'``, so that history stays and shows
them as "(deleted user)". Allows the new value in the status check. Downgrading turns them into
``removed`` (the V1 value of someone who no longer works for the practice).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "070"
down_revision: str | None = "069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_organization_members_status"), "organization_members", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_organization_members_status"),
        "organization_members",
        "status IN ('active', 'suspended', 'removed', 'inactive', 'left', 'deleted')",
    )


def downgrade() -> None:
    op.execute("UPDATE organization_members SET status = 'removed' WHERE status = 'deleted'")
    op.drop_constraint(
        op.f("ck_organization_members_status"), "organization_members", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_organization_members_status"),
        "organization_members",
        "status IN ('active', 'suspended', 'removed', 'inactive', 'left')",
    )
