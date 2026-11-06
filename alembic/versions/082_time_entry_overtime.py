"""time entry overtime

Revision ID: 082
Revises: 081

Ticket APT81, phases B-D:

* ``time_entry_overtime``: the overtime portion of a time entry, worked out from the person's
  standard hours (``staff_hours_bases``), with the overtime rate applied (``overtime_rates``),
  its cost, the premium over the standard cost, and its approval status (``pending``,
  ``approved`` or ``rejected``). One row per entry at most; it goes with the entry. Cost only:
  charge-out, WIP and invoices never read it.
* The permission ``report.staff_cost`` ("View the staff cost report"), held by the Owner, Admin
  and Partner roles.
* Members added since migration 081 without working hours get one open-ended ``weekly`` row of
  their ``weekly_capacity_minutes`` (as 081 did for everyone).

Downgrade drops the table and deletes the permission (its role grants and personal overrides
go with it). Working hours added here stay: they are ordinary rows of 081's table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers, sync_permissions

revision: str = "082"
down_revision: str | None = "081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GRANTED_TO = ("owner", "admin", "partner")


def upgrade() -> None:
    op.create_table(
        "time_entry_overtime",
        sa.Column("time_entry_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("overtime_minutes", sa.Integer(), nullable=False),
        sa.Column("allocated", sa.Boolean(), nullable=False),
        sa.Column("overtime_rate_id", sa.UUID(), nullable=True),
        sa.Column("rate_source", sa.Text(), nullable=False),
        sa.Column("overtime_rate", sa.BigInteger(), nullable=False),
        sa.Column("overtime_cost", sa.BigInteger(), nullable=False),
        sa.Column("premium_cost", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("confirmed_by", sa.UUID(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "overtime_minutes > 0", name=op.f("ck_time_entry_overtime_overtime_minutes")
        ),
        sa.CheckConstraint(
            "rate_source IN ('staff', 'grade', 'none')",
            name=op.f("ck_time_entry_overtime_rate_source"),
        ),
        sa.CheckConstraint("overtime_rate >= 0", name=op.f("ck_time_entry_overtime_overtime_rate")),
        sa.CheckConstraint("overtime_cost >= 0", name=op.f("ck_time_entry_overtime_overtime_cost")),
        sa.CheckConstraint("premium_cost >= 0", name=op.f("ck_time_entry_overtime_premium_cost")),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name=op.f("ck_time_entry_overtime_status"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "time_entry_id"],
            ["time_entries.organization_id", "time_entries.id"],
            name="fk_time_entry_overtime_time_entries",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_time_entry_overtime_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["overtime_rate_id"],
            ["overtime_rates.id"],
            name=op.f("fk_time_entry_overtime_overtime_rate_id_overtime_rates"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by"], ["users.id"], name=op.f("fk_time_entry_overtime_confirmed_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"], name=op.f("fk_time_entry_overtime_decided_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_time_entry_overtime_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_time_entry_overtime")),
        sa.UniqueConstraint("time_entry_id", name="uq_time_entry_overtime_time_entry_id"),
    )
    op.create_index(
        "ix_time_entry_overtime_organization_id_user_id",
        "time_entry_overtime",
        ["organization_id", "user_id"],
    )
    op.create_index(
        "ix_time_entry_overtime_organization_id_status",
        "time_entry_overtime",
        ["organization_id", "status"],
    )
    add_updated_at_triggers("time_entry_overtime")

    sync_permissions()
    roles = ", ".join(f"'{key}'" for key in GRANTED_TO)
    op.execute(
        f"""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id FROM roles r JOIN permissions p ON p.code = 'report.staff_cost'
        WHERE r.system_key IN ({roles})
        ON CONFLICT DO NOTHING
        """
    )

    op.execute(
        """
        INSERT INTO staff_hours_bases (
            id, organization_id, user_id, basis, standard_minutes, effective_from
        )
        SELECT gen_random_uuid(), m.organization_id, m.user_id, 'weekly',
               m.weekly_capacity_minutes,
               coalesce(m.start_date, (m.joined_at AT TIME ZONE o.timezone)::date)
        FROM organization_members m
        JOIN organizations o ON o.id = m.organization_id
        WHERE m.weekly_capacity_minutes > 0
          AND NOT EXISTS (
            SELECT 1 FROM staff_hours_bases h
            WHERE h.organization_id = m.organization_id AND h.user_id = m.user_id)
        """
    )


def downgrade() -> None:
    op.drop_table("time_entry_overtime")
    op.execute("DELETE FROM permissions WHERE code = 'report.staff_cost'")
