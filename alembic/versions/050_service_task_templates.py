"""service task templates

Revision ID: 050
Revises: 049

Adds ``service_task_templates``: the default tasks of a service (title, description, hours budget
and "due N days after the job starts"). A new job of the service can have them added as its tasks.
Additive: nothing existing changes, and the way down drops the table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "050"
down_revision: str | None = "049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "service_task_templates"


def _ck(name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{TABLE}_{name}"))


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("service_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("budget_minutes", sa.Integer(), nullable=True),
        sa.Column("due_offset_days", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        _ck("title_length", "length(title) BETWEEN 1 AND 200"),
        _ck("description_length", "description IS NULL OR length(description) <= 2000"),
        _ck("budget_minutes", "budget_minutes IS NULL OR budget_minutes >= 0"),
        _ck("due_offset_days", "due_offset_days IS NULL OR due_offset_days BETWEEN 0 AND 3650"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f(f"fk_{TABLE}_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "service_id"],
            ["services.organization_id", "services.id"],
            name="fk_service_task_templates_service_id_services",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{TABLE}")),
    )
    op.create_index(
        "ix_service_task_templates_organization_id_service_id_sort_order",
        TABLE,
        ["organization_id", "service_id", "sort_order"],
    )
    add_updated_at_triggers(TABLE)


def downgrade() -> None:
    op.drop_table(TABLE)
