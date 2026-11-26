"""email templates and the message log

``email_templates`` holds a practice's override of a built-in template (the defaults live in
code). ``message_log`` is what the mock messaging gateway recorded; its rows may belong to no
practice (account emails).

Revision ID: 014
Revises: 013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers

revision: str = "014"
down_revision: str | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_templates",
        sa.Column("template_key", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=False),
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
            "template_key ~ '^[a-z][a-z0-9_]{1,49}$'", name=op.f("ck_email_templates_key_format")
        ),
        sa.CheckConstraint(
            "length(subject) BETWEEN 1 AND 200 AND subject !~ '[[:cntrl:]]'",
            name=op.f("ck_email_templates_subject_valid"),
        ),
        sa.CheckConstraint(
            "length(html_body) BETWEEN 1 AND 100000",
            name=op.f("ck_email_templates_html_body_length"),
        ),
        sa.CheckConstraint(
            "length(text_body) BETWEEN 1 AND 50000",
            name=op.f("ck_email_templates_text_body_length"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_email_templates_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], name=op.f("fk_email_templates_updated_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_templates")),
        sa.UniqueConstraint(
            "organization_id",
            "template_key",
            name="uq_email_templates_organization_id_template_key",
        ),
    )
    add_updated_at_triggers("email_templates")

    op.create_table(
        "message_log",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=True),
        sa.Column("template_key", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("provider_ref", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.CheckConstraint("channel IN ('email', 'sms')", name=op.f("ck_message_log_channel")),
        sa.CheckConstraint("status IN ('sent', 'failed')", name=op.f("ck_message_log_status")),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_message_log_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_log")),
    )
    op.create_index(
        "ix_message_log_organization_id_created_at",
        "message_log",
        ["organization_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_message_log_created_at", "message_log", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_message_log_created_at", table_name="message_log")
    op.drop_index("ix_message_log_organization_id_created_at", table_name="message_log")
    op.drop_table("message_log")
    op.drop_table("email_templates")
