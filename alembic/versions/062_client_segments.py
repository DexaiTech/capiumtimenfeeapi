"""client segments

Revision ID: 062
Revises: 061

Adds ``segments`` (the practice's own list of client segments, managed in the settings) and
``staff_segments`` (which staff members look after the clients of a segment). Clients keep
``clients.segment`` as text: every distinct value in use today (ignoring case and surrounding
blanks) becomes a segment, so nothing a practice typed is lost and the lists agree at once.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "062"
down_revision: str | None = "061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "segments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.CheckConstraint("length(name) BETWEEN 1 AND 40", name=op.f("ck_segments_name_length")),
        sa.CheckConstraint(
            "description IS NULL OR length(description) BETWEEN 1 AND 500",
            name=op.f("ck_segments_description_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_segments_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_segments")),
        sa.UniqueConstraint("organization_id", "id", name="uq_segments_organization_id_id"),
    )
    op.create_index(
        "uq_segments_organization_id_name_lower",
        "segments",
        ["organization_id", sa.literal_column("lower(name)")],
        unique=True,
    )
    add_updated_at_triggers("segments")

    op.create_table(
        "staff_segments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("segment_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_staff_segments_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_staff_segments_organization_id_user_id_members",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "segment_id"],
            ["segments.organization_id", "segments.id"],
            name="fk_staff_segments_organization_id_segment_id_segments",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_segments")),
        sa.UniqueConstraint(
            "organization_id", "user_id", "segment_id", name="uq_staff_segments_user_id_segment_id"
        ),
    )
    op.create_index(
        "ix_staff_segments_organization_id_segment_id",
        "staff_segments",
        ["organization_id", "segment_id"],
    )

    # One segment per distinct value in use (the alphabetically first spelling of it).
    op.execute(
        """
        INSERT INTO segments (id, organization_id, name, sort_order)
        SELECT gen_random_uuid(), organization_id, name,
               (row_number() OVER (PARTITION BY organization_id ORDER BY lower(name)) - 1)
        FROM (
            SELECT organization_id, min(btrim(segment)) AS name
            FROM clients
            WHERE deleted_at IS NULL AND btrim(coalesce(segment, '')) <> ''
            GROUP BY organization_id, lower(btrim(segment))
        ) AS used
        """
    )


def downgrade() -> None:
    op.drop_table("staff_segments")
    op.drop_table("segments")
