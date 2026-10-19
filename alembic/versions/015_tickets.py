"""tickets: the Application Testing tracker

Adds ``ticket_tags``, ``tickets``, ``ticket_tag_links``, ``ticket_comments``,
``ticket_attachments`` and ``ticket_status_history``, the ``ticket`` number sequence
(``APT1``, ``APT2``, ...) and the four ``ticket.*`` permissions. Existing practices get the
sequence, the seven default tags and the permissions on their system roles.

Revision ID: 015
Revises: 014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.migration_helpers import add_updated_at_triggers, sync_permissions

revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGER_TABLES = ("ticket_tags", "tickets", "ticket_comments")
STATUSES = "'open', 'hold', 'in_progress', 'reopen', 'closed'"
PRIORITIES = "'high', 'medium', 'low', 'not_set'"
COLORS = "'violet', 'blue', 'green', 'red', 'orange', 'grey'"

DEFAULT_TAGS: tuple[tuple[str, str], ...] = (
    ("Bug", "red"),
    ("Enhancement", "violet"),
    ("UI", "blue"),
    ("Reporting", "green"),
    ("Accounts", "blue"),
    ("Validation", "red"),
    ("Trial Balance", "violet"),
)

TICKET_PERMISSIONS = ("ticket.read", "ticket.create", "ticket.update", "ticket.delete")
_ALL = TICKET_PERMISSIONS
_WORK = TICKET_PERMISSIONS[:3]
# What each system role of every existing practice is granted.
GRANTS: dict[str, tuple[str, ...]] = {
    "owner": _ALL,
    "admin": _ALL,
    "partner": _ALL,
    "manager": _ALL,
    "staff": _WORK,
    "billing": _WORK,
    "viewer": TICKET_PERMISSIONS[:1],
}


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


def _organization_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id"],
        ["organizations.id"],
        name=op.f(f"fk_{table}_organization_id_organizations"),
        ondelete="CASCADE",
    )


def _member_fk(column: str, name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id", column],
        ["organization_members.organization_id", "organization_members.user_id"],
        name=name,
    )


def _ticket_fk(name: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id", "ticket_id"],
        ["tickets.organization_id", "tickets.id"],
        name=name,
        ondelete="CASCADE",
    )


def upgrade() -> None:
    op.create_table(
        "ticket_tags",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("color", sa.Text(), server_default="grey", nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 40", name=op.f("ck_ticket_tags_name_length")
        ),
        sa.CheckConstraint(f"color IN ({COLORS})", name=op.f("ck_ticket_tags_color")),
        _organization_fk("ticket_tags"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_tags")),
        sa.UniqueConstraint("organization_id", "id", name="uq_ticket_tags_organization_id_id"),
    )
    op.create_index(
        "uq_ticket_tags_organization_id_name_lower",
        "ticket_tags",
        ["organization_id", sa.literal_column("lower(name)")],
        unique=True,
    )

    op.create_table(
        "tickets",
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), server_default="application", nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("priority", sa.Text(), server_default="not_set", nullable=False),
        sa.Column("assignee_user_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("number >= 1", name=op.f("ck_tickets_number")),
        sa.CheckConstraint("length(code) BETWEEN 1 AND 30", name=op.f("ck_tickets_code_length")),
        sa.CheckConstraint("category IN ('application')", name=op.f("ck_tickets_category")),
        sa.CheckConstraint(
            "length(subject) BETWEEN 1 AND 200", name=op.f("ck_tickets_subject_length")
        ),
        sa.CheckConstraint(
            "description IS NULL OR length(description) <= 10000",
            name=op.f("ck_tickets_description_length"),
        ),
        sa.CheckConstraint(f"status IN ({STATUSES})", name=op.f("ck_tickets_status")),
        sa.CheckConstraint(f"priority IN ({PRIORITIES})", name=op.f("ck_tickets_priority")),
        sa.CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL)", name=op.f("ck_tickets_closed_at")
        ),
        _member_fk("assignee_user_id", "fk_tickets_organization_id_assignee_members"),
        _member_fk("created_by", "fk_tickets_organization_id_created_by_members"),
        _organization_fk("tickets"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
        sa.UniqueConstraint("organization_id", "id", name="uq_tickets_organization_id_id"),
    )
    op.create_index(
        "uq_tickets_organization_id_number", "tickets", ["organization_id", "number"], unique=True
    )
    op.create_index(
        "uq_tickets_organization_id_code", "tickets", ["organization_id", "code"], unique=True
    )
    op.create_index(
        "ix_tickets_organization_id_created_at",
        "tickets",
        ["organization_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_tickets_organization_id_status",
        "tickets",
        ["organization_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_tickets_organization_id_priority",
        "tickets",
        ["organization_id", "priority", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_tickets_organization_id_assignee_user_id",
        "tickets",
        ["organization_id", "assignee_user_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("assignee_user_id IS NOT NULL"),
    )
    op.create_index(
        "ix_tickets_organization_id_subject_lower",
        "tickets",
        ["organization_id", sa.literal_column("lower(subject)"), "id"],
        unique=False,
    )

    op.create_table(
        "ticket_tag_links",
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("tag_id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _ticket_fk("fk_ticket_tag_links_ticket_id_tickets"),
        sa.ForeignKeyConstraint(
            ["organization_id", "tag_id"],
            ["ticket_tags.organization_id", "ticket_tags.id"],
            name="fk_ticket_tag_links_tag_id_ticket_tags",
            ondelete="CASCADE",
        ),
        _organization_fk("ticket_tag_links"),
        sa.PrimaryKeyConstraint("ticket_id", "tag_id", name=op.f("pk_ticket_tag_links")),
    )
    op.create_index(
        "ix_ticket_tag_links_organization_id_tag_id",
        "ticket_tag_links",
        ["organization_id", "tag_id"],
        unique=False,
    )

    op.create_table(
        "ticket_comments",
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("author_user_id", sa.UUID(), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("length(body) <= 5000", name=op.f("ck_ticket_comments_body_length")),
        _ticket_fk("fk_ticket_comments_ticket_id_tickets"),
        _member_fk("author_user_id", "fk_ticket_comments_organization_id_author_members"),
        _organization_fk("ticket_comments"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_comments")),
        sa.UniqueConstraint("organization_id", "id", name="uq_ticket_comments_organization_id_id"),
        sa.UniqueConstraint(
            "organization_id",
            "ticket_id",
            "id",
            name="uq_ticket_comments_organization_id_ticket_id_id",
        ),
    )
    op.create_index(
        "ix_ticket_comments_organization_id_ticket_id_created_at",
        "ticket_comments",
        ["organization_id", "ticket_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "ticket_attachments",
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("comment_id", sa.UUID(), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("uploaded_by", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "length(file_name) BETWEEN 1 AND 200",
            name=op.f("ck_ticket_attachments_file_name_length"),
        ),
        sa.CheckConstraint(
            "length(content_type) BETWEEN 3 AND 100",
            name=op.f("ck_ticket_attachments_content_type_length"),
        ),
        sa.CheckConstraint(
            "size_bytes BETWEEN 1 AND 2097152", name=op.f("ck_ticket_attachments_size_bytes")
        ),
        sa.CheckConstraint(
            "length(storage_key) BETWEEN 16 AND 128",
            name=op.f("ck_ticket_attachments_storage_key_length"),
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_ticket_attachments_sha256_format")
        ),
        _ticket_fk("fk_ticket_attachments_ticket_id_tickets"),
        sa.ForeignKeyConstraint(
            ["organization_id", "ticket_id", "comment_id"],
            [
                "ticket_comments.organization_id",
                "ticket_comments.ticket_id",
                "ticket_comments.id",
            ],
            name="fk_ticket_attachments_comment_id_ticket_comments",
            ondelete="CASCADE",
        ),
        _member_fk("uploaded_by", "fk_ticket_attachments_organization_id_uploader_members"),
        _organization_fk("ticket_attachments"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_attachments")),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_ticket_attachments_organization_id_id"
        ),
    )
    op.create_index(
        "uq_ticket_attachments_storage_key", "ticket_attachments", ["storage_key"], unique=True
    )
    op.create_index(
        "ix_ticket_attachments_organization_id_ticket_id",
        "ticket_attachments",
        ["organization_id", "ticket_id"],
        unique=False,
    )
    op.create_index(
        "ix_ticket_attachments_organization_id_comment_id",
        "ticket_attachments",
        ["organization_id", "comment_id"],
        unique=False,
        postgresql_where=sa.text("comment_id IS NOT NULL"),
    )

    op.create_table(
        "ticket_status_history",
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.UUID(), nullable=False),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN ({STATUSES})",
            name=op.f("ck_ticket_status_history_from_status"),
        ),
        sa.CheckConstraint(
            f"to_status IN ({STATUSES})", name=op.f("ck_ticket_status_history_to_status")
        ),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status <> to_status",
            name=op.f("ck_ticket_status_history_status_changed"),
        ),
        _ticket_fk("fk_ticket_status_history_ticket_id_tickets"),
        _member_fk("changed_by", "fk_ticket_status_history_organization_id_changed_by_members"),
        _organization_fk("ticket_status_history"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_status_history")),
    )
    op.create_index(
        "ix_ticket_status_history_organization_id_ticket_id_at",
        "ticket_status_history",
        ["organization_id", "ticket_id", "changed_at"],
        unique=False,
    )

    add_updated_at_triggers(*TRIGGER_TABLES)

    # The ticket sequence (APT1, APT2, ...): no zero padding.
    op.drop_constraint(op.f("ck_document_sequences_kind"), "document_sequences", type_="check")
    op.create_check_constraint(
        op.f("ck_document_sequences_kind"),
        "document_sequences",
        "kind IN ('client', 'job', 'invoice', 'credit_note', 'staff', 'ticket')",
    )
    op.execute(
        """
        INSERT INTO document_sequences (organization_id, kind, prefix, next_number, padding)
        SELECT id, 'ticket', 'APT', 1, 1 FROM organizations
        ON CONFLICT (organization_id, kind) DO NOTHING
        """
    )

    tags = ", ".join(f"('{name}', '{color}')" for name, color in DEFAULT_TAGS)
    op.execute(
        f"""
        INSERT INTO ticket_tags (id, organization_id, name, color)
        SELECT gen_random_uuid(), o.id, t.name, t.color
        FROM organizations o
        CROSS JOIN (VALUES {tags}) AS t(name, color)
        """
    )

    # The system roles of every existing practice (new practices copy them from the catalogue).
    sync_permissions()
    grants = ", ".join(f"('{role}', '{code}')" for role, codes in GRANTS.items() for code in codes)
    op.execute(
        f"""
        INSERT INTO role_permissions (role_id, permission_id)
        SELECT r.id, p.id
        FROM roles r
        JOIN (VALUES {grants}) AS g(system_key, code) ON g.system_key = r.system_key
        JOIN permissions p ON p.code = g.code
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    # Deleting a permission removes its grants (role_permissions cascades).
    codes = ", ".join(f"'{code}'" for code in TICKET_PERMISSIONS)
    op.execute(f"DELETE FROM permissions WHERE code IN ({codes})")

    op.execute("DELETE FROM document_sequences WHERE kind = 'ticket'")
    op.drop_constraint(op.f("ck_document_sequences_kind"), "document_sequences", type_="check")
    op.create_check_constraint(
        op.f("ck_document_sequences_kind"),
        "document_sequences",
        "kind IN ('client', 'job', 'invoice', 'credit_note', 'staff')",
    )

    op.drop_table("ticket_status_history")
    op.drop_table("ticket_attachments")
    op.drop_table("ticket_comments")
    op.drop_table("ticket_tag_links")
    op.drop_table("tickets")
    op.drop_table("ticket_tags")
