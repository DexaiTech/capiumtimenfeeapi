"""notifications: inbox rows, practice settings and personal preferences

``notifications`` holds one row per recipient and event (idempotent per ``dedupe_key``),
``notification_settings`` the practice's switch and channel defaults per type,
``notification_preferences`` a person's own choices. ``organization_members.mobile_number`` is
where SMS notifications go.

Revision ID: 013
Revises: 012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "013"
down_revision: str | None = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGER_TABLES = ("notifications", "notification_preferences", "notification_settings")
TYPE_FORMAT = "type ~ '^[a-z][a-z0-9_]{1,49}$'"


def _timestamps() -> list[sa.Column]:
    return [
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
    op.add_column("organization_members", sa.Column("mobile_number", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_organization_members_mobile_number_format"),
        "organization_members",
        "mobile_number IS NULL OR mobile_number ~ '^[+]?[0-9]{7,15}$'",
    )

    op.create_table(
        "notifications",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("link", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.UUID(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(TYPE_FORMAT, name=op.f("ck_notifications_type_format")),
        sa.CheckConstraint(
            "length(title) BETWEEN 1 AND 200", name=op.f("ck_notifications_title_length")
        ),
        sa.CheckConstraint("length(body) <= 2000", name=op.f("ck_notifications_body_length")),
        sa.CheckConstraint(
            "link IS NULL OR (length(link) <= 300 AND link ~ '^/($|[^/])' "
            "AND link !~ '[[:space:][:cntrl:]]' AND strpos(link, chr(92)) = 0)",
            name=op.f("ck_notifications_link_relative"),
        ),
        sa.CheckConstraint(
            "(entity_type IS NULL) = (entity_id IS NULL)",
            name=op.f("ck_notifications_entity_pair"),
        ),
        sa.CheckConstraint(
            "entity_type IS NULL OR length(entity_type) BETWEEN 1 AND 50",
            name=op.f("ck_notifications_entity_type_length"),
        ),
        sa.CheckConstraint(
            "dedupe_key IS NULL OR length(dedupe_key) BETWEEN 1 AND 200",
            name=op.f("ck_notifications_dedupe_key_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_notifications_organization_id_user_id_organization_members",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_notifications_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("organization_id", "id", name="uq_notifications_organization_id_id"),
    )
    op.create_index(
        "ix_notifications_organization_id_user_id_created_at",
        "notifications",
        ["organization_id", "user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_unread",
        "notifications",
        ["organization_id", "user_id"],
        unique=False,
        postgresql_where=sa.text("read_at IS NULL"),
    )
    op.create_index(
        "uq_notifications_organization_id_user_id_dedupe_key",
        "notifications",
        ["organization_id", "user_id", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("dedupe_key IS NOT NULL"),
    )

    op.create_table(
        "notification_preferences",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("in_app", sa.Boolean(), nullable=False),
        sa.Column("email", sa.Boolean(), nullable=False),
        sa.Column("sms", sa.Boolean(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(TYPE_FORMAT, name=op.f("ck_notification_preferences_type_format")),
        sa.ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            name="fk_notification_preferences_user_organization_members",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_notification_preferences_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_preferences")),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "type",
            name="uq_notification_preferences_organization_id_user_id_type",
        ),
    )

    op.create_table(
        "notification_settings",
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("default_in_app", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("default_email", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("default_sms", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(TYPE_FORMAT, name=op.f("ck_notification_settings_type_format")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_notification_settings_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], name=op.f("fk_notification_settings_updated_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_settings")),
        sa.UniqueConstraint(
            "organization_id", "type", name="uq_notification_settings_organization_id_type"
        ),
    )
    add_updated_at_triggers(*TRIGGER_TABLES)


def downgrade() -> None:
    op.drop_table("notification_settings")
    op.drop_table("notification_preferences")
    op.drop_index("uq_notifications_organization_id_user_id_dedupe_key", table_name="notifications")
    op.drop_index("ix_notifications_unread", table_name="notifications")
    op.drop_index("ix_notifications_organization_id_user_id_created_at", table_name="notifications")
    op.drop_table("notifications")
    op.drop_constraint(
        op.f("ck_organization_members_mobile_number_format"),
        "organization_members",
        type_="check",
    )
    op.drop_column("organization_members", "mobile_number")
