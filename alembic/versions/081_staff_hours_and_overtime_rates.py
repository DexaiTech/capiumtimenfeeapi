"""staff hours and overtime rates

Revision ID: 081
Revises: 080

Ticket APT81, phase A (set-up only; nothing prices time from these tables yet):

* ``staff_hours_bases``: each person's dated hours basis (``weekly`` or ``monthly``) and standard
  minutes; a person's periods never overlap (``btree_gist`` exclusion constraint). Rows go with
  the membership (``ON DELETE CASCADE``).
* ``overtime_rates``: dated overtime cost rates of a grade or a staff member, a multiplier of
  the standard cost rate (``multiplier_bp`` 10000-100000) or a fixed ``rate`` in pence per hour;
  the rates of one grade or one person never overlap.

Every member gets one open-ended ``weekly`` row of their ``weekly_capacity_minutes``, from their
start date, else the day they joined (in the practice's time zone). A member with a capacity of
0 gets none (standard minutes must be above 0).

Downgrade drops both tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "081"
down_revision: str | None = "080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NIL = "'00000000-0000-0000-0000-000000000000'::uuid"


def _common() -> list[sa.Column]:
    return [
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
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
    ]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_table(
        "staff_hours_bases",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column("standard_minutes", sa.Integer(), nullable=False),
        *_common(),
        sa.CheckConstraint(
            "basis IN ('weekly', 'monthly')", name=op.f("ck_staff_hours_bases_basis")
        ),
        sa.CheckConstraint(
            "standard_minutes > 0", name=op.f("ck_staff_hours_bases_standard_minutes")
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_staff_hours_bases_period"),
        ),
        postgresql.ExcludeConstraint(
            ("organization_id", "="),
            ("user_id", "="),
            (sa.text("daterange(effective_from, effective_to, '[]')"), "&&"),
            name="ex_staff_hours_bases_user_period",
            using="gist",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_staff_hours_bases_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_staff_hours_bases_organization_members",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_staff_hours_bases_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_hours_bases")),
    )
    add_updated_at_triggers("staff_hours_bases")

    op.create_table(
        "overtime_rates",
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("grade_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("multiplier_bp", sa.Integer(), nullable=True),
        sa.Column("rate", sa.BigInteger(), nullable=True),
        *_common(),
        sa.CheckConstraint("level IN ('grade', 'staff')", name=op.f("ck_overtime_rates_level")),
        sa.CheckConstraint(
            "(level = 'grade' AND grade_id IS NOT NULL AND user_id IS NULL) "
            "OR (level = 'staff' AND user_id IS NOT NULL AND grade_id IS NULL)",
            name=op.f("ck_overtime_rates_scope"),
        ),
        sa.CheckConstraint("mode IN ('multiplier', 'fixed')", name=op.f("ck_overtime_rates_mode")),
        sa.CheckConstraint(
            "(mode = 'multiplier' AND multiplier_bp BETWEEN 10000 AND 100000 AND rate IS NULL) "
            "OR (mode = 'fixed' AND rate >= 0 AND multiplier_bp IS NULL)",
            name=op.f("ck_overtime_rates_amount"),
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from",
            name=op.f("ck_overtime_rates_period"),
        ),
        postgresql.ExcludeConstraint(
            ("organization_id", "="),
            (sa.text(f"coalesce(user_id, {NIL})"), "="),
            (sa.text(f"coalesce(grade_id, {NIL})"), "="),
            (sa.text("daterange(effective_from, effective_to, '[]')"), "&&"),
            name="ex_overtime_rates_scope_period",
            using="gist",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_overtime_rates_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_overtime_rates_organization_id_user_id_organization_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "grade_id"],
            ["grades.organization_id", "grades.id"],
            name="fk_overtime_rates_organization_id_grade_id_grades",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_overtime_rates_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_overtime_rates")),
    )
    add_updated_at_triggers("overtime_rates")

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
        """
    )


def downgrade() -> None:
    op.drop_table("overtime_rates")
    op.drop_table("staff_hours_bases")
