"""onboarding checklist progress

Adds ``onboarding_progress``: the steps of a practice's onboarding checklist that somebody
marked done or skipped by hand (``docs/modules/onboarding.md``). Every other fact about the
checklist is derived from the practice's data, so it needs no table.

Revision ID: 036
Revises: 035
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "036"
down_revision: str | None = "035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "onboarding_progress",
        sa.Column("step_key", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
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
            "state IN ('done', 'skipped', 'todo')", name=op.f("ck_onboarding_progress_state")
        ),
        sa.CheckConstraint(
            "step_key ~ '^[a-z][a-z_]{0,39}$'", name=op.f("ck_onboarding_progress_step_key_format")
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_onboarding_progress_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_onboarding_progress_updated_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_onboarding_progress")),
        sa.UniqueConstraint(
            "organization_id", "step_key", name="uq_onboarding_progress_organization_id_step_key"
        ),
    )
    add_updated_at_triggers("onboarding_progress")


def downgrade() -> None:
    op.drop_table("onboarding_progress")
