"""expense types: what an expense claim line is for

Adds ``expense_types`` and gives every existing practice the eight default types (Travel, Mileage,
Subsistence, Accommodation, Postage, Printing, Software, Other); new practices are seeded by
``app/services/practice_defaults.py``. The default VAT code of a type is the practice's live rate
of the named kind, or none when it has no such rate.

Revision ID: 024
Revises: 023
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "024"
down_revision: str | None = "023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (name, code, VAT rate kind, is mileage, billable by default)
DEFAULT_TYPES: tuple[tuple[str, str, str, bool, bool], ...] = (
    ("Travel", "TRAVEL", "zero", False, True),
    ("Mileage", "MILEAGE", "outside_scope", True, True),
    ("Subsistence", "SUBSISTENCE", "standard", False, False),
    ("Accommodation", "ACCOM", "standard", False, True),
    ("Postage", "POSTAGE", "exempt", False, True),
    ("Printing", "PRINTING", "standard", False, True),
    ("Software", "SOFTWARE", "standard", False, False),
    ("Other", "OTHER", "standard", False, False),
)


def upgrade() -> None:
    op.create_table(
        "expense_types",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("default_vat_rate_id", sa.UUID(), nullable=True),
        sa.Column("is_mileage", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "default_billable", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
            "length(name) BETWEEN 1 AND 80", name=op.f("ck_expense_types_name_length")
        ),
        sa.CheckConstraint(
            "code ~ '^[A-Z0-9_-]{1,12}$'", name=op.f("ck_expense_types_code_format")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "default_vat_rate_id"],
            ["vat_rates.organization_id", "vat_rates.id"],
            name="fk_expense_types_default_vat_rate_id_vat_rates",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_expense_types_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_expense_types")),
        sa.UniqueConstraint("organization_id", "id", name="uq_expense_types_organization_id_id"),
    )
    op.create_index(
        "uq_expense_types_organization_id_name_lower",
        "expense_types",
        ["organization_id", sa.literal_column("lower(name)")],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_index(
        "uq_expense_types_organization_id_code",
        "expense_types",
        ["organization_id", "code"],
        unique=True,
    )
    add_updated_at_triggers("expense_types")

    types = ", ".join(
        f"('{name}', '{code}', '{vat}', {str(mileage).lower()}, {str(billable).lower()})"
        for name, code, vat, mileage, billable in DEFAULT_TYPES
    )
    op.execute(
        f"""
        INSERT INTO expense_types
            (id, organization_id, name, code, default_vat_rate_id, is_mileage, default_billable)
        SELECT gen_random_uuid(), o.id, t.name, t.code, v.id, t.is_mileage, t.billable
        FROM organizations o
        CROSS JOIN (VALUES {types}) AS t(name, code, vat_kind, is_mileage, billable)
        LEFT JOIN LATERAL (
            SELECT r.id FROM vat_rates r
            WHERE r.organization_id = o.id AND r.code = t.vat_kind AND r.archived_at IS NULL
            ORDER BY r.is_default DESC, r.created_at, r.id
            LIMIT 1
        ) v ON true
        """
    )


def downgrade() -> None:
    op.drop_table("expense_types")
