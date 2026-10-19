"""team: grades and the staff profile

Adds the grades table and the staff profile columns of ``organization_members`` (staff code,
work email, grade, working hours and days, start and leaving dates, login flags), the
``inactive`` and ``left`` member statuses and the ``staff`` number sequence. Existing practices
get staff codes (``S001``...), their members' work emails and the default grades.

Revision ID: 010
Revises: 009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.migration_helpers import add_updated_at_trigger

revision: str = "010"
down_revision: str | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (code, name, description, chargeable target in basis points, sort order)
DEFAULT_GRADES: tuple[tuple[str, str, str, int, int], ...] = (
    ("PTR", "Partner", "Owns client relationships, reviews and signs off work.", 5000, 0),
    ("MGR", "Manager", "Runs jobs and reviews the work of the team.", 7000, 1),
    ("SNR", "Senior", "Prepares and supervises complex work.", 7500, 2),
    ("JNR", "Junior", "Learns on the job under supervision.", 8000, 3),
)


def upgrade() -> None:
    op.create_table(
        "grades",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("default_cost_rate", sa.BigInteger(), nullable=True),
        sa.Column("default_charge_rate", sa.BigInteger(), nullable=True),
        sa.Column("default_chargeable_target_bp", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.CheckConstraint("code ~ '^[A-Z0-9_-]{1,12}$'", name=op.f("ck_grades_code_format")),
        sa.CheckConstraint(
            "default_chargeable_target_bp IS NULL OR default_chargeable_target_bp BETWEEN 0 AND 10000",
            name=op.f("ck_grades_default_chargeable_target_bp"),
        ),
        sa.CheckConstraint(
            "default_charge_rate IS NULL OR default_charge_rate >= 0",
            name=op.f("ck_grades_default_charge_rate"),
        ),
        sa.CheckConstraint(
            "default_cost_rate IS NULL OR default_cost_rate >= 0",
            name=op.f("ck_grades_default_cost_rate"),
        ),
        sa.CheckConstraint(
            "description IS NULL OR length(description) BETWEEN 1 AND 500",
            name=op.f("ck_grades_description_length"),
        ),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 80", name=op.f("ck_grades_name_length")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_grades_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grades")),
        sa.UniqueConstraint("organization_id", "id", name="uq_grades_organization_id_id"),
    )
    op.create_index(
        "uq_grades_organization_id_code", "grades", ["organization_id", "code"], unique=True
    )
    op.create_index(
        "uq_grades_organization_id_name_lower",
        "grades",
        ["organization_id", sa.literal_column("lower(name)")],
        unique=True,
    )
    add_updated_at_trigger("grades")

    # Staff profile columns. The code is filled in below, then made mandatory.
    op.add_column("organization_members", sa.Column("staff_code", sa.Text(), nullable=True))
    op.add_column("organization_members", sa.Column("work_email", sa.Text(), nullable=True))
    op.add_column("organization_members", sa.Column("grade_id", sa.UUID(), nullable=True))
    op.add_column(
        "organization_members",
        sa.Column(
            "working_minutes_per_day", sa.Integer(), server_default=sa.text("450"), nullable=False
        ),
    )
    op.add_column(
        "organization_members",
        sa.Column(
            "working_days",
            postgresql.ARRAY(sa.SmallInteger()),
            server_default=sa.text("'{1,2,3,4,5}'"),
            nullable=False,
        ),
    )
    op.add_column("organization_members", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column("organization_members", sa.Column("leaving_date", sa.Date(), nullable=True))
    op.add_column(
        "organization_members",
        sa.Column("login_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.add_column(
        "organization_members",
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Existing members: codes S001... in the order they joined, their login email as work email,
    # and the practice's working pattern and day length as their own.
    op.execute(
        """
        UPDATE organization_members m
        SET staff_code = 'S' || CASE WHEN r.rn < 1000 THEN lpad(r.rn::text, 3, '0')
                                     ELSE r.rn::text END,
            work_email = u.email
        FROM (
            SELECT id, row_number() OVER (PARTITION BY organization_id ORDER BY joined_at, id) AS rn
            FROM organization_members
        ) r, users u
        WHERE r.id = m.id AND u.id = m.user_id
        """
    )
    op.execute(
        """
        UPDATE organization_members m
        SET working_days = o.working_days,
            working_minutes_per_day = GREATEST(1, LEAST(1440, round(
                m.weekly_capacity_minutes::numeric / GREATEST(cardinality(o.working_days), 1)
            )::int))
        FROM organizations o
        WHERE o.id = m.organization_id
        """
    )
    op.alter_column("organization_members", "staff_code", nullable=False)

    op.drop_constraint(
        op.f("ck_organization_members_status"), "organization_members", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_organization_members_status"),
        "organization_members",
        "status IN ('active', 'suspended', 'removed', 'inactive', 'left')",
    )
    op.create_check_constraint(
        op.f("ck_organization_members_working_minutes"),
        "organization_members",
        "working_minutes_per_day BETWEEN 1 AND 1440",
    )
    op.create_check_constraint(
        op.f("ck_organization_members_working_days"),
        "organization_members",
        "cardinality(working_days) BETWEEN 1 AND 7 "
        "AND working_days <@ ARRAY[1, 2, 3, 4, 5, 6, 7]::smallint[]",
    )
    op.create_check_constraint(
        op.f("ck_organization_members_leaving_after_start"),
        "organization_members",
        "leaving_date IS NULL OR start_date IS NULL OR leaving_date >= start_date",
    )
    op.create_check_constraint(
        op.f("ck_organization_members_staff_code_format"),
        "organization_members",
        "staff_code ~ '^[A-Z0-9_-]{1,20}$'",
    )
    op.create_check_constraint(
        op.f("ck_organization_members_work_email_length"),
        "organization_members",
        "work_email IS NULL OR length(work_email) BETWEEN 3 AND 254",
    )
    op.create_foreign_key(
        "fk_organization_members_organization_id_grade_id_grades",
        "organization_members",
        "grades",
        ["organization_id", "grade_id"],
        ["organization_id", "id"],
    )
    op.create_index(
        "ix_organization_members_organization_id_grade_id",
        "organization_members",
        ["organization_id", "grade_id"],
        unique=False,
    )
    op.create_index(
        "uq_organization_members_organization_id_staff_code",
        "organization_members",
        ["organization_id", sa.literal_column("upper(staff_code)")],
        unique=True,
    )
    op.create_index(
        "uq_organization_members_organization_id_work_email_active",
        "organization_members",
        ["organization_id", sa.literal_column("lower(work_email)")],
        unique=True,
        postgresql_where=sa.text("work_email IS NOT NULL AND status = 'active'"),
    )

    # The staff number sequence: S001, S002, ... (next number follows the codes just given).
    op.drop_constraint(op.f("ck_document_sequences_kind"), "document_sequences", type_="check")
    op.create_check_constraint(
        op.f("ck_document_sequences_kind"),
        "document_sequences",
        "kind IN ('client', 'job', 'invoice', 'credit_note', 'staff')",
    )
    op.execute(
        """
        INSERT INTO document_sequences (organization_id, kind, prefix, next_number, padding)
        SELECT o.id, 'staff', 'S', COALESCE(c.n, 0) + 1, 3
        FROM organizations o
        LEFT JOIN (
            SELECT organization_id, count(*) AS n FROM organization_members GROUP BY organization_id
        ) c ON c.organization_id = o.id
        """
    )

    grades = ", ".join(
        f"('{code}', '{name}', '{description}', {target}, {order})"
        for code, name, description, target, order in DEFAULT_GRADES
    )
    op.execute(
        f"""
        INSERT INTO grades (
            id, organization_id, code, name, description, default_chargeable_target_bp, sort_order
        )
        SELECT gen_random_uuid(), o.id, g.code, g.name, g.description, g.target, g.sort_order
        FROM organizations o
        CROSS JOIN (VALUES {grades}) AS g(code, name, description, target, sort_order)
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM document_sequences WHERE kind = 'staff'")
    op.drop_constraint(op.f("ck_document_sequences_kind"), "document_sequences", type_="check")
    op.create_check_constraint(
        op.f("ck_document_sequences_kind"),
        "document_sequences",
        "kind IN ('client', 'job', 'invoice', 'credit_note')",
    )

    op.execute("UPDATE organization_members SET status = 'suspended' WHERE status = 'inactive'")
    op.execute("UPDATE organization_members SET status = 'removed' WHERE status = 'left'")
    op.drop_index(
        "uq_organization_members_organization_id_work_email_active",
        table_name="organization_members",
        postgresql_where=sa.text("work_email IS NOT NULL AND status = 'active'"),
    )
    op.drop_index(
        "uq_organization_members_organization_id_staff_code", table_name="organization_members"
    )
    op.drop_index(
        "ix_organization_members_organization_id_grade_id", table_name="organization_members"
    )
    op.drop_constraint(
        "fk_organization_members_organization_id_grade_id_grades",
        "organization_members",
        type_="foreignkey",
    )
    for name in (
        "work_email_length",
        "staff_code_format",
        "leaving_after_start",
        "working_days",
        "working_minutes",
    ):
        op.drop_constraint(
            op.f(f"ck_organization_members_{name}"), "organization_members", type_="check"
        )
    op.drop_constraint(
        op.f("ck_organization_members_status"), "organization_members", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_organization_members_status"),
        "organization_members",
        "status IN ('active', 'suspended', 'removed')",
    )
    for column in (
        "invited_at",
        "login_enabled",
        "leaving_date",
        "start_date",
        "working_days",
        "working_minutes_per_day",
        "grade_id",
        "work_email",
        "staff_code",
    ):
        op.drop_column("organization_members", column)

    op.drop_index("uq_grades_organization_id_name_lower", table_name="grades")
    op.drop_index("uq_grades_organization_id_code", table_name="grades")
    op.drop_table("grades")
